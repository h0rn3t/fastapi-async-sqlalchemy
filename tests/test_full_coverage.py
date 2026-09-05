"""Targeted tests covering the last middleware branches that aren't reachable
via normal end-to-end flows — race windows, defensive error paths, direct
contextvar manipulation, and the SQLModel-not-installed import fallback."""

import asyncio
import importlib.util
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi_async_sqlalchemy import create_middleware_and_session_proxy
from fastapi_async_sqlalchemy.exceptions import MissingSessionError

DB_URL = "sqlite+aiosqlite://"


def _get_closure_var(db_obj, var_name: str):
    """Search closures across every callable attribute of the proxy class and
    its metaclass to find a free variable by name. The proxy factory captures
    different vars in different methods."""
    seen: set[int] = set()

    def _candidates():
        for src in (db_obj, type(db_obj)):
            for value in src.__dict__.values():
                fn = value.fget if isinstance(value, property) else value
                if callable(fn) and id(fn) not in seen:
                    seen.add(id(fn))
                    yield fn

    for func in _candidates():
        code = getattr(func, "__code__", None)
        closure = getattr(func, "__closure__", None)
        if code is None or closure is None:
            continue
        if var_name in code.co_freevars:
            return closure[code.co_freevars.index(var_name)].cell_contents
    raise KeyError(var_name)


@pytest.mark.asyncio
async def test_request_session_access_after_streaming_close_raises():
    """db.session access after the request session is marked closed-for-streaming
    raises a clear RuntimeError."""
    Middleware, _db = create_middleware_and_session_proxy()
    Middleware(app=None, db_url=DB_URL)

    # The flag lives on the request-context object rather than in a ContextVar,
    # so that a child task (any `@app.middleware("http")` runs the app in one)
    # sees the same state the middleware wrote.
    request_context = _db(_request_context=True)
    await request_context.__aenter__()
    try:
        request_context._closed_for_streaming = True
        with pytest.raises(RuntimeError, match="closed for streaming"):
            _ = _db.session
    finally:
        await request_context.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_closed_for_streaming_state_reaches_a_child_task():
    """A task started before the close must still see it.

    `BaseHTTPMiddleware` runs the application — and therefore any streaming body
    generator — in a child task, which inherits a *copy* of the context. A
    ContextVar set by the middleware afterwards would be invisible there, so the
    generator would hit a bare SQLAlchemy error instead of the explanation.
    """
    Middleware, _db = create_middleware_and_session_proxy()
    Middleware(app=None, db_url=DB_URL)

    request_context = _db(_request_context=True)
    await request_context.__aenter__()
    errors = []
    resume = asyncio.Event()

    async def touch_session_after_the_close():
        await resume.wait()
        try:
            _ = _db.session
        except RuntimeError as exc:
            errors.append(str(exc))

    # Started *before* the close, exactly as the body generator's task is.
    child = asyncio.create_task(touch_session_after_the_close())
    try:
        await request_context.close_request_session_for_streaming()
        resume.set()
        await child
    finally:
        await request_context.__aexit__(None, None, None)

    assert len(errors) == 1
    assert "closed for streaming" in errors[0]


@pytest.mark.asyncio
async def test_connection_releases_slot_when_parent_closes_during_acquire():
    """If the parent multi-session context starts closing while a waiter is
    parked on semaphore.acquire(), the waiter must release the slot it just
    obtained and raise (middleware.py lines 180-181)."""
    Middleware, _db = create_middleware_and_session_proxy()
    Middleware(app=None, db_url=DB_URL)

    holder_started = asyncio.Event()
    holder_release = asyncio.Event()

    async def holder():
        async with _db.connection() as session:
            holder_started.set()
            await session.execute(text("SELECT 1"))
            await holder_release.wait()

    multi_state_var = _get_closure_var(_db, "_multi_state")

    async with _db(multi_sessions=True, max_concurrent=1):
        holder_task = asyncio.create_task(holder())
        await holder_started.wait()

        async def waiter():
            async with _db.connection():
                pass

        waiter_task = asyncio.create_task(waiter())
        # Let the waiter park on acquire().
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Manually flip closing so the post-acquire re-check (line 179) raises.
        state = multi_state_var.get()
        state.closing = True

        # Release the holder so the waiter wakes up, finds closing=True,
        # releases the slot it just took, and raises.
        holder_release.set()
        await holder_task

        with pytest.raises(RuntimeError, match="started closing"):
            await waiter_task

        state.closing = False


@pytest.mark.asyncio
async def test_finalize_regular_session_raises_when_session_missing():
    """If the bound session is reset to None before __aexit__, finalize must
    raise MissingSessionError (middleware.py line 668)."""
    Middleware, db_obj = create_middleware_and_session_proxy()
    Middleware(app=None, db_url=DB_URL)

    session_var = _get_closure_var(db_obj, "_session")

    ctx = db_obj()
    await ctx.__aenter__()

    # Yank the session out from under the context — exercises the defensive
    # `if session is None: raise MissingSessionError` path.
    real_session = session_var.get()
    drop_token = session_var.set(None)
    try:
        with pytest.raises(MissingSessionError):
            await ctx.__aexit__(None, None, None)
    finally:
        session_var.reset(drop_token)
        await real_session.close()


@pytest.mark.asyncio
async def test_waiters_cancelled_when_context_exits_with_holder_active():
    """Tasks parked on db.connection()'s semaphore at the moment the parent
    multi-session context exits must be cancelled by __aexit__ via the
    `state.waiters` sweep (middleware.py lines 703 & 705)."""
    Middleware, _db = create_middleware_and_session_proxy()
    Middleware(app=None, db_url=DB_URL)

    holder_started = asyncio.Event()
    holder_release = asyncio.Event()
    waiter_started = asyncio.Event()

    async def holder():
        async with _db.connection() as session:
            holder_started.set()
            await session.execute(text("SELECT 1"))
            await holder_release.wait()

    async def waiter():
        waiter_started.set()
        async with _db.connection():
            pass

    holder_task: asyncio.Task | None = None
    waiter_task: asyncio.Task | None = None
    try:
        async with _db(multi_sessions=True, max_concurrent=1):
            holder_task = asyncio.create_task(holder())
            await holder_started.wait()

            waiter_task = asyncio.create_task(waiter())
            await waiter_started.wait()
            # Park the waiter on acquire().
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            # Exit the multi-session context with the holder still active and
            # the waiter still parked — __aexit__ must walk state.waiters and
            # cancel them.
    finally:
        holder_release.set()
        if holder_task is not None:
            await asyncio.gather(holder_task, return_exceptions=True)
        if waiter_task is not None:
            await asyncio.gather(waiter_task, return_exceptions=True)

    assert waiter_task is not None
    assert waiter_task.cancelled()


def test_middleware_falls_back_to_sqlalchemy_session_when_sqlmodel_missing():
    """Re-execute middleware.py source in a fresh namespace with sqlmodel
    blocked, exercising the ImportError fallback path (middleware.py line 28)."""
    middleware_path = sys.modules["fastapi_async_sqlalchemy.middleware"].__file__
    assert middleware_path is not None

    # Block sqlmodel and its parent packages — sys.modules[name] = None makes
    # `import name` raise ModuleNotFoundError without affecting installed pkgs.
    saved_modules = {}
    for name in list(sys.modules):
        if name == "sqlmodel" or name.startswith("sqlmodel."):
            saved_modules[name] = sys.modules.pop(name)

    sys.modules["sqlmodel"] = None  # type: ignore[assignment]
    sys.modules["sqlmodel.ext"] = None  # type: ignore[assignment]
    sys.modules["sqlmodel.ext.asyncio"] = None  # type: ignore[assignment]
    sys.modules["sqlmodel.ext.asyncio.session"] = None  # type: ignore[assignment]

    clone_name = "_fasq_sqlmodel_missing_clone"
    try:
        spec = importlib.util.spec_from_file_location(clone_name, middleware_path)
        assert spec is not None and spec.loader is not None
        clone = importlib.util.module_from_spec(spec)
        # Register in sys.modules before exec so dataclass annotation resolution
        # (sys.modules[cls.__module__]) finds the live module during class body.
        sys.modules[clone_name] = clone
        spec.loader.exec_module(clone)

        # Without sqlmodel, the fallback assigns AsyncSession directly.
        assert clone.DefaultAsyncSession is AsyncSession
    finally:
        sys.modules.pop(clone_name, None)
        sys.modules.pop("sqlmodel", None)
        sys.modules.pop("sqlmodel.ext", None)
        sys.modules.pop("sqlmodel.ext.asyncio", None)
        sys.modules.pop("sqlmodel.ext.asyncio.session", None)
        for name, mod in saved_modules.items():
            sys.modules[name] = mod


@pytest.mark.asyncio
async def test_buffered_response_start_is_forwarded_when_no_body_arrives():
    """A response that never sends a body still gets its buffered start out.

    The middleware buffers `http.response.start` until the body ends, so an app
    that returns without ever sending one would otherwise swallow the response
    whole. It is flushed once the app call returns.
    """
    Middleware, _db = create_middleware_and_session_proxy()

    async def start_only_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})

    middleware = Middleware(start_only_app, db_url=DB_URL)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await middleware({"type": "http", "path": "/", "headers": []}, receive, send)

    assert sent == [{"type": "http.response.start", "status": 204, "headers": []}]
