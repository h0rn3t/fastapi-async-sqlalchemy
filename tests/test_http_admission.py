import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from fastapi_async_sqlalchemy import create_middleware_and_session_proxy
from fastapi_async_sqlalchemy.exceptions import MissingSessionError


async def receive():
    return {"type": "http.request", "body": b""}


async def invoke(middleware, path="/work"):
    messages = []

    async def send(message):
        messages.append(message)

    await middleware({"type": "http", "path": path}, receive, send)
    return messages


@pytest.mark.asyncio
async def test_shared_http_limit_rejects_overload_and_bypasses_health(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admission.db'}")
    middleware_class, db = create_middleware_and_session_proxy()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def app(scope, receive, send):
        calls.append(scope["path"])
        if scope["path"] == "/health":
            with pytest.raises(MissingSessionError):
                _ = db.session
        else:
            await db.session.execute(text("SELECT 1"))
            started.set()
            await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = middleware_class(
        app,
        custom_engine=engine,
        max_concurrent_requests=1,
        request_queue_timeout=0.02,
        exclude_paths=["/health"],
    )
    first = asyncio.create_task(invoke(middleware))
    try:
        await asyncio.wait_for(started.wait(), 2)
        rejected = await invoke(middleware)
        assert rejected[0]["status"] == 503
        assert (b"retry-after", b"1") in rejected[0]["headers"]
        assert calls == ["/work"]
        health = await invoke(middleware, "/health")
        assert health[0]["status"] == 200
        release.set()
        assert (await first)[0]["status"] == 200
        assert (await invoke(middleware))[0]["status"] == 200
    finally:
        release.set()
        await first
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_holder", [False, True])
async def test_cancelled_http_requests_do_not_leak_permits(cancel_holder):
    middleware_class, _ = create_middleware_and_session_proxy()
    started = asyncio.Event()
    release = asyncio.Event()

    async def app(scope, receive, send):
        started.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = middleware_class(
        app,
        db_url="sqlite+aiosqlite://",
        max_concurrent_requests=1,
        request_queue_timeout=1,
    )
    holder = asyncio.create_task(invoke(middleware))
    waiter = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        waiter = asyncio.create_task(invoke(middleware))
        await asyncio.sleep(0)
        cancelled = holder if cancel_holder else waiter
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        release.set()
        survivor = waiter if cancel_holder else holder
        assert (await survivor)[0]["status"] == 200
        assert (await invoke(middleware))[0]["status"] == 200
    finally:
        release.set()
        await asyncio.gather(*(t for t in (holder, waiter) if t), return_exceptions=True)
        await middleware.dispose()


@pytest.mark.asyncio
async def test_background_does_not_hold_http_permit():
    from starlette.background import BackgroundTask
    from starlette.responses import JSONResponse

    middleware_class, _ = create_middleware_and_session_proxy()
    background_started = asyncio.Event()
    release = asyncio.Event()

    async def background():
        background_started.set()
        await release.wait()

    async def app(scope, receive, send):
        task = BackgroundTask(background) if scope["path"] == "/background" else None
        await JSONResponse({"ok": True}, background=task)(scope, receive, send)

    middleware = middleware_class(
        app,
        db_url="sqlite+aiosqlite://",
        max_concurrent_requests=1,
        request_queue_timeout=0.02,
    )
    first = asyncio.create_task(invoke(middleware, "/background"))
    try:
        await asyncio.wait_for(background_started.wait(), 2)
        assert (await invoke(middleware))[0]["status"] == 200
    finally:
        release.set()
        await first
        await middleware.dispose()


@pytest.mark.parametrize(
    "kwargs",
    [{"max_concurrent_requests": value} for value in (0, -1, 1.5, True)]
    + [{"request_queue_timeout": value} for value in (0, -1, float("nan"), float("inf"))],
)
def test_invalid_admission_configuration(kwargs):
    middleware_class, _ = create_middleware_and_session_proxy()
    with pytest.raises(ValueError):
        middleware_class(None, db_url="sqlite+aiosqlite://", **kwargs)
