import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse

from fastapi_async_sqlalchemy import create_middleware_and_session_proxy


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_on_exit", [False, True])
async def test_background_task_releases_request_connection_before_running(tmp_path, commit_on_exit):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'background.db'}",
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.1,
    )
    middleware_class, db = create_middleware_and_session_proxy()
    messages = []
    background_started = asyncio.Event()
    release_background = asyncio.Event()
    request_task = None

    async def background():
        background_started.set()
        await release_background.wait()

    async def app(scope, receive, send):
        await db.session.execute(text("SELECT 1"))
        await JSONResponse({"ok": True}, background=BackgroundTask(background))(
            scope, receive, send
        )

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        messages.append(message)

    middleware = middleware_class(app, custom_engine=engine, commit_on_exit=commit_on_exit)
    try:
        request_task = asyncio.create_task(middleware({"type": "http"}, receive, send))
        await asyncio.wait_for(background_started.wait(), timeout=2)
        assert engine.pool.checkedout() == 0
        assert [message["type"] for message in messages] == [
            "http.response.start",
            "http.response.body",
        ]
        async with db():
            assert (await db.session.execute(text("SELECT 3"))).scalar() == 3
    finally:
        release_background.set()
        if request_task is not None:
            await request_task
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("background_fails", [False, True])
async def test_background_owns_transaction_and_cannot_reopen_request_session(
    tmp_path, background_fails
):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'transactions.db'}",
        pool_size=1,
        max_overflow=0,
    )
    middleware_class, db = create_middleware_and_session_proxy()
    messages = []
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE entries (value INTEGER)"))

    async def background():
        with pytest.raises(RuntimeError, match="closed after response"):
            _ = db.session
        # Check the response was sent and the request write committed already.
        assert len(messages) == 2
        async with db(commit_on_exit=True):
            assert (await db.session.execute(text("SELECT value FROM entries"))).scalar() == 1
            await db.session.execute(text("INSERT INTO entries VALUES (2)"))
            if background_fails:
                raise ValueError("background failed")

    async def app(scope, receive, send):
        await db.session.execute(text("INSERT INTO entries VALUES (1)"))
        await JSONResponse({"ok": True}, background=BackgroundTask(background))(
            scope, receive, send
        )

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        messages.append(message)

    middleware = middleware_class(app, custom_engine=engine, commit_on_exit=True)
    try:
        if background_fails:
            with pytest.raises(ValueError, match="background failed"):
                await middleware({"type": "http"}, receive, send)
        else:
            await middleware({"type": "http"}, receive, send)
        async with engine.connect() as conn:
            rows = (
                (await conn.execute(text("SELECT value FROM entries ORDER BY value")))
                .scalars()
                .all()
            )
            assert rows == ([1] if background_fails else [1, 2])
    finally:
        await engine.dispose()
