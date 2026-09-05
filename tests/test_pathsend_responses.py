"""Regression tests for the ASGI `http.response.pathsend` extension.

When the server supports pathsend, `FileResponse` hands over a path instead of
a body and then runs its `BackgroundTask` — typically the one that deletes the
temporary file it just served. The middleware used to buffer the pathsend
message like any unknown one and forward it only after the application had
returned, so the file was already gone by the time the server read it, and the
request's database connection stayed checked out for the whole background task.
Per the ASGI spec, pathsend ends the response body.
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import AsyncAdaptedQueuePool
from starlette.background import BackgroundTask

from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

DB_URL = "sqlite+aiosqlite://"


def _add_passthrough_middleware(app):
    """Install a `BaseHTTPMiddleware` below the SQLAlchemy one."""

    @app.middleware("http")
    async def passthrough(request: Request, call_next):
        return await call_next(request)


async def _call_with_pathsend(app, path="/file"):
    """Drive *app* over raw ASGI, advertising the pathsend extension."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "extensions": {"http.response.pathsend": {}},
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    return messages


@pytest.mark.asyncio
async def test_pathsend_file_still_exists_when_the_server_receives_it(tmp_path):
    served = tmp_path / "report.csv"
    served.write_text("value\n1\n")
    existed_at_send = {}

    Middleware, db = create_middleware_and_session_proxy()
    app = FastAPI()
    app.add_middleware(Middleware, db_url=DB_URL)

    def delete_file():
        served.unlink()

    @app.get("/file")
    async def download():
        await db.session.execute(text("SELECT 1"))
        return FileResponse(served, background=BackgroundTask(delete_file))

    # The middleware forwards pathsend to *its* caller; record the file state at
    # that moment, which is when a real server would open the path.
    async def observing_app(scope, receive, send):
        async def observing_send(message):
            if message["type"] == "http.response.pathsend":
                existed_at_send["exists"] = served.exists()
            await send(message)

        await app(scope, receive, observing_send)

    messages = await _call_with_pathsend(observing_app)

    assert [m["type"] for m in messages] == [
        "http.response.start",
        "http.response.pathsend",
    ]
    assert messages[1]["path"] == str(served)
    assert existed_at_send == {"exists": True}, "the background task deleted the file first"
    assert not served.exists(), "the background task should still have run"


@pytest.mark.asyncio
async def test_pathsend_returns_the_connection_before_background_tasks(tmp_path):
    """The request session must be finalized before the background task runs."""
    served = tmp_path / "report.csv"
    served.write_text("value\n1\n")
    checked_out_during_background = {}

    Middleware, db = create_middleware_and_session_proxy()
    app = FastAPI()
    app.add_middleware(
        Middleware,
        db_url="sqlite+aiosqlite:///",
        engine_args={"poolclass": AsyncAdaptedQueuePool, "pool_size": 1, "max_overflow": 0},
    )

    def record_pool():
        checked_out_during_background["checked_out"] = db.pool_status()["checked_out"]

    @app.get("/file")
    async def download():
        await db.session.execute(text("SELECT 1"))
        assert db.pool_status()["checked_out"] == 1
        return FileResponse(served, background=BackgroundTask(record_pool))

    messages = await _call_with_pathsend(app)

    assert [m["type"] for m in messages] == [
        "http.response.start",
        "http.response.pathsend",
    ]
    assert checked_out_during_background == {"checked_out": 0}, (
        "the request connection was pinned for the whole background task"
    )


@pytest.mark.asyncio
async def test_pathsend_commits_before_the_response_is_forwarded(tmp_path):
    """`commit_on_exit` writes must land, and fail the response if they can't."""
    served = tmp_path / "report.csv"
    served.write_text("value\n1\n")

    Middleware, db = create_middleware_and_session_proxy()
    app = FastAPI()
    app.add_middleware(Middleware, db_url=DB_URL, commit_on_exit=True)
    events = []

    @app.get("/file")
    async def download():
        session = db.session
        original_commit = session.commit

        async def tracking_commit():
            events.append("commit")
            await original_commit()

        session.commit = tracking_commit
        await session.execute(text("SELECT 1"))
        return FileResponse(served)

    async def recording_app(scope, receive, send):
        async def recording_send(message):
            if message["type"] == "http.response.start":
                events.append("response_start")
            await send(message)

        await app(scope, receive, recording_send)

    await _call_with_pathsend(recording_app)

    assert events == ["commit", "response_start"]


# ---------------------------------------------------------------------------
# Ordering of finalization, the forwarded message, and the background task
#
# Without an inner middleware the ordering is guaranteed, not lucky:
# `FileResponse.__call__` awaits its `send` and only then runs `self.background()`,
# so nothing can start the background task while the middleware finalizes.
#
# A `BaseHTTPMiddleware` below breaks that guarantee for every outer middleware,
# not just this one: it hands the message to a queue in one task while the
# application task carries on to `self.background()`. Anything the outer
# middleware awaits — a commit here — lets the background task run first. The
# tests below pin down what is guaranteed and record what is not, so neither
# can change silently.
# ---------------------------------------------------------------------------


async def _pathsend_ordering(tmp_path, *, inner_middleware, commit_on_exit=False):
    served = tmp_path / "report.csv"
    served.write_text("value\n1\n")
    events = []

    Middleware, db = create_middleware_and_session_proxy()
    app = FastAPI()

    def delete_file():
        events.append("background")
        served.unlink()

    @app.get("/file")
    async def download():
        session = db.session
        original_commit = session.commit

        async def tracking_commit():
            events.append("commit")
            await original_commit()

        session.commit = tracking_commit
        await session.execute(text("SELECT 1"))
        return FileResponse(served, background=BackgroundTask(delete_file))

    if inner_middleware:
        _add_passthrough_middleware(app)

    app.add_middleware(Middleware, db_url=DB_URL, commit_on_exit=commit_on_exit)

    async def observing_app(scope, receive, send):
        async def observing_send(message):
            if message["type"] == "http.response.pathsend":
                events.append(f"send(exists={served.exists()})")
            await send(message)

        await app(scope, receive, observing_send)

    await _call_with_pathsend(observing_app)
    return events


@pytest.mark.asyncio
async def test_pathsend_ordering_is_guaranteed_without_an_inner_middleware(tmp_path):
    events = await _pathsend_ordering(tmp_path, inner_middleware=False, commit_on_exit=True)

    assert events == ["commit", "send(exists=True)", "background"]


@pytest.mark.asyncio
async def test_pathsend_background_races_finalization_behind_an_inner_middleware(tmp_path):
    """Documents a Starlette-level limitation, not an intended behaviour.

    `BaseHTTPMiddleware` decouples the application task from the outer send, so
    the background task runs while the commit is still in flight. Sending the
    message before finalizing would only narrow the window, and would cost the
    guarantee that a failing commit still stops the response — see the test
    below. The connection is therefore released late here, and a background
    task that deletes the served file can win the race.
    """
    events = await _pathsend_ordering(tmp_path, inner_middleware=True, commit_on_exit=True)
    send_event = next(event for event in events if event.startswith("send("))

    # The ordering the middleware still controls.
    assert events[0] == "commit", events
    # The ordering that is lost: the background task no longer follows the send.
    # Should this ever start passing, the limitation documented in
    # docs/guide/http-load.md is gone and should be removed with this assertion.
    assert events.index("background") < events.index(send_event), events


@pytest.mark.asyncio
async def test_pathsend_commit_failure_still_prevents_a_200(tmp_path):
    """Holds with an inner middleware too: `http.response.start` stays buffered."""
    served = tmp_path / "report.csv"
    served.write_text("value\n1\n")

    Middleware, db = create_middleware_and_session_proxy()
    app = FastAPI()

    @app.get("/file")
    async def download():
        session = db.session

        async def failing_commit():
            raise SQLAlchemyError("commit failed before the file was handed over")

        session.commit = failing_commit
        await session.execute(text("SELECT 1"))
        return FileResponse(served)

    _add_passthrough_middleware(app)
    app.add_middleware(Middleware, db_url=DB_URL, commit_on_exit=True)

    sent = []

    async def capturing_app(scope, receive, send):
        async def capturing_send(message):
            sent.append(message)
            await send(message)

        await app(scope, receive, capturing_send)

    with pytest.raises(SQLAlchemyError, match="commit failed"):
        await _call_with_pathsend(capturing_app)

    # `ServerErrorMiddleware` sits outside this middleware and turns the raised
    # commit error into a 500 before re-raising, so the only response start that
    # may escape is that one — never the buffered 200, and never the pathsend.
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
    ]
    assert sent[0]["status"] == 500


@pytest.mark.asyncio
async def test_pathsend_releases_the_admission_slot(tmp_path):
    """`max_concurrent_requests` must not hold its slot for background tasks.

    The slot is released when the response body ends, and a pathsend ends it —
    otherwise a slow background task keeps the next request queueing behind a
    response that is already fully handed over.
    """
    served = tmp_path / "report.csv"
    served.write_text("value\n1\n")
    slot_state = {}

    Middleware, db = create_middleware_and_session_proxy()
    app = FastAPI()

    def record_slot():
        slot_state["held"] = middleware._request_semaphore.locked()

    @app.get("/file")
    async def download():
        await db.session.execute(text("SELECT 1"))
        return FileResponse(served, background=BackgroundTask(record_slot))

    middleware = Middleware(app, db_url=DB_URL, max_concurrent_requests=1)

    messages = await _call_with_pathsend(middleware)

    assert [m["type"] for m in messages] == [
        "http.response.start",
        "http.response.pathsend",
    ]
    assert slot_state == {"held": False}, "the admission slot outlived the response"
