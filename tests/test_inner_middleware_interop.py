"""The middleware must behave the same with other middlewares stacked below it.

`@app.middleware("http")` installs a `BaseHTTPMiddleware`, which runs the
application in a child task and re-emits even a fully buffered response as a
chunk flagged `more_body=True` plus a terminating empty one — so from the
outside every response looks streamed. A compressing middleware below it also
drops the `content-length` that would otherwise have revealed the real body
size. Guessing "is this a stream?" from those messages is therefore impossible,
and the guesses this middleware used to make turned ordinary JSON responses
into rolled-back writes or 500s.
"""

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.middleware.gzip import GZipMiddleware

from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

# Big enough that GZipMiddleware compresses it and switches to a streamed,
# `content-length`-free response.
PAYLOAD = ["x" * 100] * 20


async def _make_app(tmp_path, *, gzip, http_middleware, commit_on_exit=False):
    """Build `SQLAlchemyMiddleware [-> GZip] [-> BaseHTTP] -> app`.

    `add_middleware` prepends, so the last one added ends up outermost.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'interop.db'}")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE entries (value INTEGER)"))

    middleware_class, db = create_middleware_and_session_proxy()
    app = FastAPI()

    if http_middleware:

        @app.middleware("http")
        async def passthrough(request: Request, call_next):
            return await call_next(request)

    if gzip:
        app.add_middleware(GZipMiddleware, minimum_size=1)

    app.add_middleware(middleware_class, custom_engine=engine, commit_on_exit=commit_on_exit)
    return engine, db, app


async def _rows(engine):
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT value FROM entries ORDER BY value"))
        return result.scalars().all()


@pytest.mark.parametrize("gzip", [False, True], ids=["plain", "gzip"])
@pytest.mark.parametrize("http_middleware", [False, True], ids=["direct", "http_middleware"])
@pytest.mark.asyncio
async def test_commit_on_exit_write_lands_for_every_stack(tmp_path, gzip, http_middleware):
    """A plain JSON endpoint must work under all four combinations."""
    engine, db, app = await _make_app(
        tmp_path,
        gzip=gzip,
        http_middleware=http_middleware,
        commit_on_exit=True,
    )

    @app.post("/entries")
    async def create_entry():
        await db.session.execute(text("INSERT INTO entries VALUES (3)"))
        return {"items": PAYLOAD}

    try:
        with TestClient(app) as client:
            response = client.post("/entries", headers={"accept-encoding": "gzip"})

        assert response.status_code == 200
        assert response.json() == {"items": PAYLOAD}
        assert await _rows(engine) == [3]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_commit_precedes_response_start_behind_gzip_and_http_middleware(tmp_path):
    """The buffered 200 is still released only after the commit succeeds."""
    engine, db, app = await _make_app(
        tmp_path,
        gzip=True,
        http_middleware=True,
        commit_on_exit=True,
    )
    events = []

    @app.post("/entries")
    async def create_entry():
        session = db.session
        original_commit = session.commit

        async def tracking_commit():
            events.append("commit")
            await original_commit()

        session.commit = tracking_commit
        await session.execute(text("INSERT INTO entries VALUES (4)"))
        return {"items": PAYLOAD}

    async def recording_app(scope, receive, send):
        async def recording_send(message):
            if message["type"] == "http.response.start":
                events.append("response_start")
            await send(message)

        await app(scope, receive, recording_send)

    try:
        with TestClient(recording_app) as client:
            assert client.post("/entries", headers={"accept-encoding": "gzip"}).status_code == 200

        assert events == ["commit", "response_start"]
        assert await _rows(engine) == [4]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_yield_dependency_transaction_survives_gzip_and_http_middleware(tmp_path):
    """An owned transaction is still left to its owner through both layers."""
    engine, db, app = await _make_app(tmp_path, gzip=True, http_middleware=True)

    async def transaction():
        async with db.session.begin():
            yield

    @app.post("/entries", dependencies=[Depends(transaction)])
    async def create_entry():
        await db.session.execute(text("INSERT INTO entries VALUES (5)"))
        return {"items": PAYLOAD}

    try:
        with TestClient(app) as client:
            assert client.post("/entries", headers={"accept-encoding": "gzip"}).status_code == 200

        assert await _rows(engine) == [5], "the dependency's commit was rolled back"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_streaming_generator_behind_http_middleware_gets_the_explanation(tmp_path):
    """The generator runs in the child task and must still see the close.

    This is what the shared request-context object buys: a ContextVar set by
    the middleware after the child task started would never reach it.
    """
    engine, db, app = await _make_app(
        tmp_path,
        gzip=False,
        http_middleware=True,
        commit_on_exit=True,
    )
    errors = []

    @app.get("/stream")
    async def stream():
        async def body():
            yield b"first\n"
            try:
                await db.session.execute(text("SELECT 1"))
            except RuntimeError as exc:
                errors.append(str(exc))
            yield b"second\n"

        return StreamingResponse(body(), media_type="text/plain")

    try:
        with TestClient(app) as client:
            response = client.get("/stream")

        assert response.status_code == 200
        assert response.text == "first\nsecond\n"
        assert len(errors) == 1
        assert "closed for streaming" in errors[0]
    finally:
        await engine.dispose()
