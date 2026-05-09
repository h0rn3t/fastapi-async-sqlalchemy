import asyncio
import inspect
import warnings

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

DB_URL = "sqlite+aiosqlite://"


def _make_middleware_and_db():
    from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

    return create_middleware_and_session_proxy()


def _get_ctx_var(_db, var_name: str):
    session_prop = type(_db).__dict__["session"]
    closure = {
        name: cell.cell_contents
        for name, cell in zip(
            session_prop.fget.__code__.co_freevars,
            session_prop.fget.__closure__,
        )
    }
    return closure[var_name]


def _capture_http_messages(app, messages):
    async def wrapped(scope, receive, send):
        async def capture_send(message):
            if message["type"].startswith("http.response"):
                messages.append(message.copy())
            await send(message)

        await app(scope, receive, capture_send)

    return wrapped


def test_commit_failure_prevents_successful_response_start():
    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url=DB_URL, commit_on_exit=True)
    messages = []

    @app.get("/commit-fails")
    async def commit_fails():
        session = db.session

        async def failing_commit():
            raise SQLAlchemyError("commit failed before response start")

        session.commit = failing_commit
        await session.execute(text("SELECT 1"))
        return {"ok": True}

    with TestClient(
        _capture_http_messages(app, messages),
        raise_server_exceptions=False,
    ) as client:
        response = client.get("/commit-fails")

    assert response.status_code == 500
    assert not any(
        message["type"] == "http.response.start" and message["status"] == 200
        for message in messages
    )


def test_successful_commit_precedes_normal_response_start():
    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url=DB_URL, commit_on_exit=True)
    events = []

    async def recording_app(scope, receive, send):
        async def recording_send(message):
            if message["type"] == "http.response.start":
                events.append("response_start")
            await send(message)

        await app(scope, receive, recording_send)

    @app.get("/commits-first")
    async def commits_first():
        session = db.session
        original_commit = session.commit

        async def tracking_commit():
            events.append("commit")
            await original_commit()

        session.commit = tracking_commit
        await session.execute(text("SELECT 1"))
        return {"ok": True}

    with TestClient(recording_app) as client:
        response = client.get("/commits-first")

    assert response.status_code == 200
    assert events == ["commit", "response_start"]


def test_explicit_streaming_read_owns_and_closes_body_session():
    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url=DB_URL, commit_on_exit=True)
    closed = []

    @app.get("/stream")
    async def stream():
        async def body():
            async with db():
                session = db.session
                original_close = session.close

                async def tracking_close():
                    closed.append("closed")
                    await original_close()

                session.close = tracking_close

                for value in range(3):
                    result = await db.session.execute(text(f"SELECT {value}"))
                    yield f"{result.scalar()}\n".encode()

        return StreamingResponse(body(), media_type="text/plain")

    with TestClient(app) as client:
        response = client.get("/stream")

    assert response.status_code == 200
    assert response.text == "0\n1\n2\n"
    assert closed == ["closed"]


def test_implicit_commit_on_exit_streaming_write_is_not_reported_successful():
    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url=DB_URL, commit_on_exit=True)
    messages = []

    @app.get("/stream-write")
    async def stream_write():
        async def body():
            await db.session.execute(text("CREATE TABLE unsafe_stream (value INTEGER)"))
            await db.session.execute(text("INSERT INTO unsafe_stream VALUES (1)"))
            yield b"created\n"

        return StreamingResponse(body(), media_type="text/plain")

    with TestClient(
        _capture_http_messages(app, messages),
        raise_server_exceptions=False,
    ) as client:
        response = client.get("/stream-write")

    assert response.status_code == 500
    assert not any(
        message["type"] == "http.response.start" and message["status"] == 200
        for message in messages
    )


def test_non_database_streaming_closes_request_session_before_response_start(monkeypatch):
    import fastapi_async_sqlalchemy.middleware as middleware_module

    SQLAlchemyMiddleware, _db = _make_middleware_and_db()
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url=DB_URL, commit_on_exit=True)
    events = []
    original_close = middleware_module.DefaultAsyncSession.close

    async def tracking_close(self, *args, **kwargs):
        events.append("request_session_close")
        await original_close(self, *args, **kwargs)

    monkeypatch.setattr(middleware_module.DefaultAsyncSession, "close", tracking_close)

    async def recording_app(scope, receive, send):
        async def recording_send(message):
            if message["type"] == "http.response.start":
                events.append("response_start")
            await send(message)

        await app(scope, receive, recording_send)

    @app.get("/plain-stream")
    async def plain_stream():
        async def body():
            yield b"one\n"
            yield b"two\n"

        return StreamingResponse(body(), media_type="text/plain")

    with TestClient(recording_app) as client:
        response = client.get("/plain-stream")

    assert response.status_code == 200
    assert response.text == "one\ntwo\n"
    assert events.index("request_session_close") < events.index("response_start")


def test_owned_db_url_engine_is_disposed_on_lifespan_shutdown(monkeypatch):
    import fastapi_async_sqlalchemy.middleware as middleware_module

    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    app = FastAPI()
    engine = create_async_engine(DB_URL)
    dispose_calls = []
    original_dispose = AsyncEngine.dispose

    async def dispose_spy(self, *args, **kwargs):
        dispose_calls.append(self)
        return await original_dispose(self, *args, **kwargs)

    monkeypatch.setattr(AsyncEngine, "dispose", dispose_spy)
    monkeypatch.setattr(middleware_module, "create_async_engine", lambda *_, **__: engine)

    app.add_middleware(SQLAlchemyMiddleware, db_url=DB_URL)

    @app.get("/")
    async def get_value():
        result = await db.session.execute(text("SELECT 1"))
        return {"value": result.scalar()}

    with TestClient(app) as client:
        assert client.get("/").json() == {"value": 1}

    assert dispose_calls == [engine]


def test_owned_engine_is_disposed_on_shutdown_failed(monkeypatch):
    from contextlib import asynccontextmanager

    import fastapi_async_sqlalchemy.middleware as middleware_module

    SQLAlchemyMiddleware, _db = _make_middleware_and_db()
    engine = create_async_engine(DB_URL)
    dispose_calls = []
    original_dispose = AsyncEngine.dispose

    async def dispose_spy(self, *args, **kwargs):
        dispose_calls.append(self)
        return await original_dispose(self, *args, **kwargs)

    monkeypatch.setattr(AsyncEngine, "dispose", dispose_spy)
    monkeypatch.setattr(middleware_module, "create_async_engine", lambda *_, **__: engine)

    @asynccontextmanager
    async def failing_lifespan(_app):
        yield
        raise RuntimeError("user shutdown blew up")

    app = FastAPI(lifespan=failing_lifespan)
    app.add_middleware(SQLAlchemyMiddleware, db_url=DB_URL)

    with pytest.raises(RuntimeError, match="user shutdown blew up"):
        with TestClient(app):
            pass

    assert dispose_calls == [engine]


def test_owned_engine_is_disposed_on_startup_failed(monkeypatch):
    from contextlib import asynccontextmanager

    import fastapi_async_sqlalchemy.middleware as middleware_module

    SQLAlchemyMiddleware, _db = _make_middleware_and_db()
    engine = create_async_engine(DB_URL)
    dispose_calls = []
    original_dispose = AsyncEngine.dispose

    async def dispose_spy(self, *args, **kwargs):
        dispose_calls.append(self)
        return await original_dispose(self, *args, **kwargs)

    monkeypatch.setattr(AsyncEngine, "dispose", dispose_spy)
    monkeypatch.setattr(middleware_module, "create_async_engine", lambda *_, **__: engine)

    @asynccontextmanager
    async def failing_startup(_app):
        raise RuntimeError("startup blew up")
        yield  # unreachable, but required so this is a valid asynccontextmanager

    app = FastAPI(lifespan=failing_startup)
    app.add_middleware(SQLAlchemyMiddleware, db_url=DB_URL)

    with pytest.raises(RuntimeError, match="startup blew up"):
        with TestClient(app):
            pass

    assert dispose_calls == [engine]


@pytest.mark.asyncio
async def test_custom_engine_is_not_disposed_on_shutdown_failed(monkeypatch):
    from contextlib import asynccontextmanager

    SQLAlchemyMiddleware, _db = _make_middleware_and_db()
    engine = create_async_engine(DB_URL)
    dispose_calls = []
    original_dispose = AsyncEngine.dispose

    async def dispose_spy(self, *args, **kwargs):
        dispose_calls.append(self)
        return await original_dispose(self, *args, **kwargs)

    monkeypatch.setattr(AsyncEngine, "dispose", dispose_spy)

    @asynccontextmanager
    async def failing_lifespan(_app):
        yield
        raise RuntimeError("user shutdown blew up")

    app = FastAPI(lifespan=failing_lifespan)
    app.add_middleware(SQLAlchemyMiddleware, custom_engine=engine)

    try:
        with pytest.raises(RuntimeError, match="user shutdown blew up"):
            with TestClient(app):
                pass

        assert engine not in dispose_calls
    finally:
        await original_dispose(engine)


@pytest.mark.asyncio
async def test_owned_engine_disposal_is_idempotent(monkeypatch):
    import fastapi_async_sqlalchemy.middleware as middleware_module

    SQLAlchemyMiddleware, _db = _make_middleware_and_db()
    app = FastAPI()
    engine = create_async_engine(DB_URL)
    dispose_calls = []
    original_dispose = AsyncEngine.dispose

    async def dispose_spy(self, *args, **kwargs):
        dispose_calls.append(self)
        return await original_dispose(self, *args, **kwargs)

    monkeypatch.setattr(AsyncEngine, "dispose", dispose_spy)
    monkeypatch.setattr(middleware_module, "create_async_engine", lambda *_, **__: engine)

    middleware = SQLAlchemyMiddleware(app, db_url=DB_URL)

    await middleware.dispose()
    await middleware.dispose()

    assert dispose_calls == [engine]


@pytest.mark.asyncio
async def test_custom_engine_is_not_disposed_on_lifespan_shutdown(monkeypatch):
    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    app = FastAPI()
    engine = create_async_engine(DB_URL)
    dispose_calls = []
    original_dispose = AsyncEngine.dispose

    async def dispose_spy(self, *args, **kwargs):
        dispose_calls.append(self)
        return await original_dispose(self, *args, **kwargs)

    monkeypatch.setattr(AsyncEngine, "dispose", dispose_spy)

    app.add_middleware(SQLAlchemyMiddleware, custom_engine=engine)

    @app.get("/")
    async def get_value():
        result = await db.session.execute(text("SELECT 2"))
        return {"value": result.scalar()}

    try:
        with TestClient(app) as client:
            assert client.get("/").json() == {"value": 2}

        assert engine not in dispose_calls
    finally:
        await original_dispose(engine)


@pytest.mark.asyncio
async def test_same_proxy_rejects_reinitialization_with_different_live_engine():
    SQLAlchemyMiddleware, _db = _make_middleware_and_db()
    app = FastAPI()
    engine_one = create_async_engine(DB_URL)
    engine_two = create_async_engine(DB_URL)

    try:
        SQLAlchemyMiddleware(app, custom_engine=engine_one)

        with pytest.raises(RuntimeError, match="create_middleware_and_session_proxy"):
            SQLAlchemyMiddleware(app, custom_engine=engine_two)
    finally:
        await engine_one.dispose()
        await engine_two.dispose()


@pytest.mark.asyncio
async def test_independent_proxies_keep_separate_engine_bindings():
    FirstMiddleware, first_db = _make_middleware_and_db()
    SecondMiddleware, second_db = _make_middleware_and_db()
    app = FastAPI()
    first_engine = create_async_engine(DB_URL)
    second_engine = create_async_engine(DB_URL)

    try:
        FirstMiddleware(app, custom_engine=first_engine)
        SecondMiddleware(app, custom_engine=second_engine)

        async with first_db(commit_on_exit=True):
            await first_db.session.execute(text("CREATE TABLE marker (value TEXT)"))
            await first_db.session.execute(text("INSERT INTO marker VALUES ('first')"))

        async with second_db(commit_on_exit=True):
            await second_db.session.execute(text("CREATE TABLE marker (value TEXT)"))
            await second_db.session.execute(text("INSERT INTO marker VALUES ('second')"))

        async with first_db():
            first_value = (
                await first_db.session.execute(text("SELECT value FROM marker"))
            ).scalar_one()

        async with second_db():
            second_value = (
                await second_db.session.execute(text("SELECT value FROM marker"))
            ).scalar_one()

        assert first_value == "first"
        assert second_value == "second"
    finally:
        await first_engine.dispose()
        await second_engine.dispose()


@pytest.mark.asyncio
async def test_throttled_gather_fail_fast_closes_unopened_coroutines():
    # Scheduling-order assumption: with max_concurrent=1, only `first` can hold
    # the semaphore slot at any time. When it raises, fail-fast cancels the
    # `second`/`third` tasks before they ever enter their throttled wrapper —
    # so their underlying coroutines must remain unstarted (CORO_CREATED) and
    # be `coro.close()`'d explicitly by db.gather() rather than awaited. If
    # this scheduling assumption ever breaks (e.g. a future scheduler runs
    # `_throttled` for second/third synchronously), the assertion must still
    # hold — it is the contract — but the path that satisfies it changes.
    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    SQLAlchemyMiddleware(FastAPI(), db_url=DB_URL)

    async def fail_fast():
        raise RuntimeError("boom")

    async def never_opened():
        await asyncio.sleep(0)
        return "not reached"

    first = fail_fast()
    second = never_opened()
    third = never_opened()

    with pytest.raises(RuntimeError, match="boom"):
        async with db(multi_sessions=True, max_concurrent=1):
            await db.gather(first, second, third)

    assert inspect.getcoroutinestate(second) == inspect.CORO_CLOSED
    assert inspect.getcoroutinestate(third) == inspect.CORO_CLOSED


@pytest.mark.asyncio
async def test_throttled_gather_rejects_precreated_task_and_future():
    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    SQLAlchemyMiddleware(FastAPI(), db_url=DB_URL)
    loop = asyncio.get_running_loop()
    task = loop.create_task(asyncio.sleep(10))
    future = loop.create_future()

    try:
        async with db(multi_sessions=True, max_concurrent=1):
            with pytest.raises(TypeError, match="coroutine objects"):
                await db.gather(task)

            with pytest.raises(TypeError, match="coroutine objects"):
                await db.gather(future)
    finally:
        task.cancel()
        future.cancel()
        await asyncio.gather(task, future, return_exceptions=True)


@pytest.mark.asyncio
async def test_child_session_creation_is_rejected_after_multi_state_starts_closing():
    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    SQLAlchemyMiddleware(FastAPI(), db_url=DB_URL)
    multi_state_var = _get_ctx_var(db, "_multi_state")

    async with db(multi_sessions=True):
        state = multi_state_var.get()
        assert state is not None
        state.closing = True

        async def use_db_after_cleanup_started():
            return db.session

        with pytest.raises(RuntimeError, match="closing"):
            await asyncio.create_task(use_db_after_cleanup_started())


@pytest.mark.asyncio
async def test_rejected_db_url_reinit_does_not_allocate_engine(monkeypatch):
    """A rejected `db_url` re-init must not allocate a fresh engine."""
    import fastapi_async_sqlalchemy.middleware as middleware_module

    SQLAlchemyMiddleware, _db = _make_middleware_and_db()
    app = FastAPI()
    first_engine = create_async_engine(DB_URL)

    try:
        SQLAlchemyMiddleware(app, custom_engine=first_engine)

        create_calls = []

        def fail_if_called(*args, **kwargs):
            create_calls.append((args, kwargs))
            raise AssertionError(
                "create_async_engine must not be called when proxy re-init is rejected"
            )

        monkeypatch.setattr(middleware_module, "create_async_engine", fail_if_called)

        with pytest.raises(RuntimeError, match="create_middleware_and_session_proxy"):
            SQLAlchemyMiddleware(app, db_url=DB_URL)

        assert create_calls == []
    finally:
        await first_engine.dispose()


@pytest.mark.asyncio
async def test_dispose_failure_clears_bindings_and_allows_retry(monkeypatch):
    """If engine.dispose() raises, bindings clear and a retry actually re-runs it."""
    import fastapi_async_sqlalchemy.middleware as middleware_module

    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    engine = create_async_engine(DB_URL)
    monkeypatch.setattr(middleware_module, "create_async_engine", lambda *_, **__: engine)

    middleware = SQLAlchemyMiddleware(FastAPI(), db_url=DB_URL)

    # _Session_engine / _Session are nonlocals in the proxy factory closure;
    # the middleware's `dispose` method closes over both, so we can inspect
    # the same cells through it.
    closure = dict(
        zip(
            middleware.dispose.__func__.__code__.co_freevars,
            middleware.dispose.__func__.__closure__,
        )
    )
    assert closure["_Session_engine"].cell_contents is engine
    assert closure["_Session"].cell_contents is not None

    real_dispose = AsyncEngine.dispose
    call_count = {"n": 0}

    async def flaky_dispose(self, *args, **kwargs):
        if self is engine:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("dispose boom")
        return await real_dispose(self, *args, **kwargs)

    monkeypatch.setattr(AsyncEngine, "dispose", flaky_dispose)

    with pytest.raises(RuntimeError, match="dispose boom"):
        await middleware.dispose()

    # Bindings cleared by the finally block.
    assert closure["_Session_engine"].cell_contents is None
    assert closure["_Session"].cell_contents is None

    # _engine_disposed must NOT be set on failure, so a retry actually re-runs.
    assert middleware._engine_disposed is False

    # Retry: now succeeds and underlying dispose is invoked again.
    await middleware.dispose()
    assert call_count["n"] == 2
    assert middleware._engine_disposed is True


@pytest.mark.asyncio
async def test_lifespan_ack_is_forwarded_when_dispose_raises(monkeypatch):
    """ASGI lifespan ack must be forwarded even if engine disposal raises."""
    import fastapi_async_sqlalchemy.middleware as middleware_module

    SQLAlchemyMiddleware, _db = _make_middleware_and_db()
    engine = create_async_engine(DB_URL)
    monkeypatch.setattr(middleware_module, "create_async_engine", lambda *_, **__: engine)

    sent_messages = []
    shutdown_sent = []

    async def receive():
        # First call: initiate shutdown. Subsequent calls would park, but the
        # inner_app acks immediately so this branch is unreachable in practice.
        if not shutdown_sent:
            shutdown_sent.append(True)
            return {"type": "lifespan.shutdown"}
        await asyncio.Event().wait()  # pragma: no cover

    async def send(message):
        sent_messages.append(message)

    async def inner_app(_scope, receive_, send_):
        # Minimal lifespan handler: receive shutdown, send shutdown.complete.
        msg = await receive_()
        assert msg["type"] == "lifespan.shutdown"
        await send_({"type": "lifespan.shutdown.complete"})

    middleware = SQLAlchemyMiddleware(inner_app, db_url=DB_URL)

    async def failing_dispose():
        raise RuntimeError("dispose fail")

    monkeypatch.setattr(middleware, "dispose", failing_dispose)

    with pytest.raises(RuntimeError, match="dispose fail"):
        await middleware({"type": "lifespan"}, receive, send)

    assert sent_messages == [{"type": "lifespan.shutdown.complete"}]

    # Cleanup: real engine still needs disposal.
    await engine.dispose()


@pytest.mark.asyncio
async def test_throttled_gather_cancellation_emits_no_resource_warning():
    """db.gather() fail-fast must not emit `coroutine ... was never awaited`."""
    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    SQLAlchemyMiddleware(FastAPI(), db_url=DB_URL)

    async def fail_fast():
        raise RuntimeError("boom")

    async def never_opened():
        await asyncio.sleep(0)
        return "not reached"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        first = fail_fast()
        second = never_opened()
        third = never_opened()

        with pytest.raises(RuntimeError, match="boom"):
            async with db(multi_sessions=True, max_concurrent=1):
                await db.gather(first, second, third)

    leak_warnings = [
        w
        for w in caught
        if issubclass(w.category, ResourceWarning) and "was never awaited" in str(w.message)
    ]
    assert leak_warnings == [], (
        f"Unexpected ResourceWarning(s): {[str(w.message) for w in leak_warnings]}"
    )
