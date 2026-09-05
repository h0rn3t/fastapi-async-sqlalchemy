"""Tests for pool fail-fast, pool observability and excluded paths.

These cover the failure mode where a saturated pool turns a cheap endpoint
(typically a readiness probe) into a request that parks for the engine-wide
``pool_timeout``, so the pod fails its probe and is pulled out of load
balancing while the application itself reports nothing.
"""

import asyncio
import logging
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

db_url = "sqlite+aiosqlite:///"


def _ensure_modules():
    for mod_name in ("fastapi_async_sqlalchemy", "fastapi_async_sqlalchemy.middleware"):
        if mod_name not in sys.modules:
            __import__(mod_name)


@pytest.fixture(autouse=True)
def _restore_modules():
    _ensure_modules()
    yield
    _ensure_modules()


def _make_pair(**engine_kw):
    """Create a middleware/db pair on a tiny, saturable pool."""
    _ensure_modules()
    from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

    Middleware, _db = create_middleware_and_session_proxy()
    engine_args = {
        "poolclass": AsyncAdaptedQueuePool,
        "pool_size": 1,
        "max_overflow": 0,
        "pool_timeout": 30,
    }
    engine_args.update(engine_kw)
    middleware = Middleware(app=None, db_url=db_url, engine_args=engine_args)
    return middleware, _db


# ---------------------------------------------------------------------------
# db(pool_timeout=...) — fail fast instead of parking on the engine deadline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_timeout_raises_instead_of_waiting():
    """A saturated pool must fail within the context deadline, not pool_timeout."""
    from fastapi_async_sqlalchemy import PoolTimeoutError

    middleware, _db = _make_pair(pool_timeout=30)

    hold = await middleware.engine.connect()
    await hold.execute(text("SELECT 1"))
    try:
        started = asyncio.get_running_loop().time()
        with pytest.raises(PoolTimeoutError) as exc_info:
            async with _db(pool_timeout=0.2):
                pass
        elapsed = asyncio.get_running_loop().time() - started
    finally:
        await hold.close()

    assert elapsed < 5, f"waited {elapsed}s — the engine pool_timeout won instead"
    assert exc_info.value.timeout == 0.2
    assert exc_info.value.retry_after >= 1
    assert exc_info.value.pool_status["checked_out"] == 1


@pytest.mark.asyncio
async def test_pool_timeout_is_a_timeout_error():
    """PoolTimeoutError stays catchable by plain `except TimeoutError`."""
    from fastapi_async_sqlalchemy import PoolTimeoutError

    assert issubclass(PoolTimeoutError, TimeoutError)


@pytest.mark.asyncio
async def test_pool_timeout_succeeds_when_pool_has_room():
    """With a free pool the deadline is invisible — the session works normally."""
    _, _db = _make_pair()

    async with _db(pool_timeout=5):
        result = await _db.session.execute(text("SELECT 7"))
        assert result.scalar() == 7


@pytest.mark.asyncio
async def test_pool_timeout_checks_out_eagerly():
    """A context deadline forces checkout on entry, not on first query."""
    middleware, _db = _make_pair()

    async with _db(pool_timeout=5):
        assert middleware.engine.pool.checkedout() == 1

    assert middleware.engine.pool.checkedout() == 0


@pytest.mark.asyncio
async def test_failed_checkout_does_not_leak_connections():
    """Repeated timed-out checkouts must not lose pool slots."""
    from fastapi_async_sqlalchemy import PoolTimeoutError

    middleware, _db = _make_pair()
    pool = middleware.engine.pool

    hold = await middleware.engine.connect()
    await hold.execute(text("SELECT 1"))
    try:
        for _ in range(25):
            with pytest.raises(PoolTimeoutError):
                async with _db(pool_timeout=0.05):
                    pass
    finally:
        await hold.close()

    assert pool.checkedout() == 0

    # The pool must still be fully usable afterwards.
    async with _db():
        result = await _db.session.execute(text("SELECT 42"))
        assert result.scalar() == 42
    assert pool.checkedout() == 0


@pytest.mark.asyncio
async def test_failed_checkout_leaves_no_dangling_session_context():
    """__aexit__ never runs after a failed __aenter__, so state must stay clean."""
    from fastapi_async_sqlalchemy import PoolTimeoutError
    from fastapi_async_sqlalchemy.exceptions import MissingSessionError

    middleware, _db = _make_pair()

    hold = await middleware.engine.connect()
    await hold.execute(text("SELECT 1"))
    try:
        with pytest.raises(PoolTimeoutError):
            async with _db(pool_timeout=0.05):
                pass
    finally:
        await hold.close()

    with pytest.raises(MissingSessionError):
        _ = _db.session


@pytest.mark.asyncio
async def test_pool_timeout_must_be_positive():
    _, _db = _make_pair()

    with pytest.raises(ValueError, match="`pool_timeout` must be greater than 0"):
        async with _db(pool_timeout=0):
            pass


# ---------------------------------------------------------------------------
# db.connection(timeout=...)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_timeout_raises_on_saturated_pool():
    from fastapi_async_sqlalchemy import PoolTimeoutError

    middleware, _db = _make_pair()

    hold = await middleware.engine.connect()
    await hold.execute(text("SELECT 1"))
    try:
        async with _db(multi_sessions=True):
            with pytest.raises(PoolTimeoutError):
                async with _db.connection(timeout=0.05):
                    pass
    finally:
        await hold.close()

    assert middleware.engine.pool.checkedout() == 0


@pytest.mark.asyncio
async def test_connection_timeout_releases_semaphore_slot():
    """A failed checkout must give its throttling slot back."""
    from fastapi_async_sqlalchemy import PoolTimeoutError

    middleware, _db = _make_pair()

    hold = await middleware.engine.connect()
    await hold.execute(text("SELECT 1"))
    try:
        async with _db(multi_sessions=True, max_concurrent=1):
            for _ in range(3):
                with pytest.raises(PoolTimeoutError):
                    async with _db.connection(timeout=0.05):
                        pass
    finally:
        await hold.close()

    # A slot leak would have deadlocked the loop above on the second attempt.
    async with _db(multi_sessions=True, max_concurrent=1):
        async with _db.connection(timeout=5) as session:
            result = await session.execute(text("SELECT 3"))
            assert result.scalar() == 3


@pytest.mark.asyncio
async def test_connection_inherits_context_pool_timeout():
    """db.connection() with no explicit timeout uses the enclosing context's."""
    from fastapi_async_sqlalchemy import PoolTimeoutError

    middleware, _db = _make_pair()

    hold = await middleware.engine.connect()
    await hold.execute(text("SELECT 1"))
    try:
        async with _db(multi_sessions=True, pool_timeout=0.05):
            with pytest.raises(PoolTimeoutError):
                async with _db.connection():
                    pass
    finally:
        await hold.close()


@pytest.mark.asyncio
async def test_connection_timeout_does_not_close_borrowed_session():
    """In non-multi mode the session belongs to the outer context."""
    from fastapi_async_sqlalchemy import PoolTimeoutError

    middleware, _db = _make_pair(pool_size=2)

    async with _db():
        outer = _db.session
        hold_a = await middleware.engine.connect()
        hold_b = await middleware.engine.connect()
        try:
            with pytest.raises(PoolTimeoutError):
                async with _db.connection(timeout=0.05):
                    pass
        finally:
            await hold_a.close()
            await hold_b.close()

        # The borrowed session survived the failed checkout.
        assert _db.session is outer
        result = await outer.execute(text("SELECT 11"))
        assert result.scalar() == 11


@pytest.mark.asyncio
async def test_engine_level_timeout_is_reported_as_pool_timeout():
    """When the engine deadline is the shorter one, it surfaces the same way."""
    from fastapi_async_sqlalchemy import PoolTimeoutError

    middleware, _db = _make_pair(pool_timeout=0.05)

    hold = await middleware.engine.connect()
    await hold.execute(text("SELECT 1"))
    try:
        with pytest.raises(PoolTimeoutError) as exc_info:
            async with _db(pool_timeout=10):
                pass
    finally:
        await hold.close()

    from sqlalchemy import exc as sa_exc

    assert isinstance(exc_info.value.__cause__, sa_exc.TimeoutError)


@pytest.mark.asyncio
async def test_connection_timeout_on_reused_task_session():
    """A nested db.connection() reuses the task's session and honours a deadline."""
    _, _db = _make_pair(pool_size=2)

    async with _db(multi_sessions=True, max_concurrent=2):

        async def work():
            async with _db.connection() as first:
                async with _db.connection(timeout=5) as second:
                    assert second is first
                    result = await second.execute(text("SELECT 13"))
                    return result.scalar()

        assert await asyncio.create_task(work()) == 13


@pytest.mark.asyncio
async def test_engine_deadline_shorter_than_context_deadline_is_not_rewrapped():
    """A failure raised before the context deadline expires passes through as-is."""
    from fastapi_async_sqlalchemy import PoolTimeoutError

    middleware, _db = _make_pair(pool_timeout=0.01)

    hold = await middleware.engine.connect()
    await hold.execute(text("SELECT 1"))
    try:
        async with _db(multi_sessions=True):
            started = asyncio.get_running_loop().time()
            with pytest.raises(PoolTimeoutError):
                async with _db.connection(timeout=5):
                    pass
            elapsed = asyncio.get_running_loop().time() - started
    finally:
        await hold.close()

    assert elapsed < 1, f"waited {elapsed}s — the 5s context deadline should not have been reached"
    assert middleware.engine.pool.checkedout() == 0


@pytest.mark.asyncio
async def test_connection_timeout_must_be_positive():
    _, _db = _make_pair()

    with pytest.raises(ValueError, match="`timeout` must be greater than 0"):
        _db.connection(timeout=-1)


@pytest.mark.asyncio
async def test_gather_honours_pool_timeout_without_max_concurrent():
    """A context deadline must apply even when no semaphore is configured.

    Delegating straight to asyncio.gather() let the coroutines check out their
    own connections and park on the engine deadline instead of the context's.
    """
    from fastapi_async_sqlalchemy import PoolTimeoutError

    middleware, _db = _make_pair(pool_timeout=5)

    hold = await middleware.engine.connect()
    await hold.execute(text("SELECT 1"))
    try:
        async with _db(multi_sessions=True, pool_timeout=0.05):

            async def work():
                result = await _db.session.execute(text("SELECT 1"))
                return result.scalar()

            started = asyncio.get_running_loop().time()
            results = await _db.gather(work(), work(), return_exceptions=True)
            elapsed = asyncio.get_running_loop().time() - started
    finally:
        await hold.close()

    assert all(isinstance(r, PoolTimeoutError) for r in results), results
    assert elapsed < 2, f"waited {elapsed}s — the engine pool_timeout won instead"


@pytest.mark.asyncio
async def test_gather_rejects_pre_created_task_under_pool_timeout():
    """Wrapping applies under pool_timeout, so pre-started inputs are rejected."""
    _, _db = _make_pair()

    async def work():
        return 1

    async with _db(multi_sessions=True, pool_timeout=5):
        task = asyncio.create_task(work())
        try:
            with pytest.raises(TypeError, match="`pool_timeout` is set"):
                await _db.gather(task)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_gather_inherits_context_pool_timeout():
    """db.gather() honours the context deadline for every wrapped coroutine."""
    from fastapi_async_sqlalchemy import PoolTimeoutError

    middleware, _db = _make_pair()

    hold = await middleware.engine.connect()
    await hold.execute(text("SELECT 1"))
    try:
        async with _db(multi_sessions=True, max_concurrent=2, pool_timeout=0.05):

            async def work():
                return 1

            results = await _db.gather(work(), work(), return_exceptions=True)
            assert all(isinstance(r, PoolTimeoutError) for r in results)
    finally:
        await hold.close()


def _closure_var(db_obj, var_name: str):
    """Read a closure variable of the proxy's `session` property."""
    session_prop = type(db_obj).__dict__["session"]
    closure = {
        name: cell.cell_contents
        for name, cell in zip(
            session_prop.fget.__code__.co_freevars,
            session_prop.fget.__closure__,
            strict=False,
        )
    }
    return closure[var_name]


@pytest.mark.asyncio
async def test_connection_rejected_after_context_started_closing():
    """A deadline must not let a session slip in once the owner is closing."""
    _, _db = _make_pair()

    async with _db(multi_sessions=True, max_concurrent=1):
        state = _closure_var(_db, "_multi_state").get()
        assert state is not None
        state.closing = True
        try:
            with pytest.raises(RuntimeError, match="started closing"):
                async with _db.connection(timeout=5):
                    pass
        finally:
            state.closing = False


@pytest.mark.asyncio
async def test_context_closing_during_checkout_is_rejected():
    """The owner can start closing while a task is parked on checkout."""
    middleware, _db = _make_pair()

    hold = await middleware.engine.connect()
    await hold.execute(text("SELECT 1"))

    async with _db(multi_sessions=True):
        state = _closure_var(_db, "_multi_state").get()
        assert state is not None

        entered = asyncio.Event()

        async def worker():
            entered.set()
            async with _db.connection(timeout=5) as session:
                await session.execute(text("SELECT 1"))

        task = asyncio.create_task(worker())
        await entered.wait()
        await asyncio.sleep(0.05)  # let the worker park on the checkout

        state.closing = True
        await hold.close()  # the checkout can now succeed — but the owner is closing

        try:
            with pytest.raises(RuntimeError, match="closing"):
                await task
        finally:
            state.closing = False

    assert middleware.engine.pool.checkedout() == 0


@pytest.mark.asyncio
async def test_user_owns_transaction_tolerates_a_missing_session():
    """The finalization guard runs on every response; it must never raise."""
    _ensure_modules()
    from fastapi_async_sqlalchemy.middleware import _user_owns_transaction

    assert _user_owns_transaction(None) is False
    assert _user_owns_transaction(object()) is False


# ---------------------------------------------------------------------------
# db.pool_status()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_status_reports_saturation():
    middleware, _db = _make_pair(pool_size=2, max_overflow=1)

    status = _db.pool_status()
    assert status["pool_class"] == "AsyncAdaptedQueuePool"
    assert status["size"] == 2
    assert status["max_overflow"] == 1
    assert status["capacity"] == 3
    assert status["checked_out"] == 0
    assert status["available"] == 3
    assert status["saturation"] == 0

    held = [await middleware.engine.connect() for _ in range(3)]
    try:
        status = _db.pool_status()
        assert status["checked_out"] == 3
        assert status["available"] == 0
        assert status["saturation"] == 1.0
    finally:
        for connection in held:
            await connection.close()


@pytest.mark.asyncio
async def test_pool_status_tolerates_pools_without_counters():
    """NullPool/StaticPool track nothing — report None, don't crash."""
    _ensure_modules()
    from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

    Middleware, _db = create_middleware_and_session_proxy()
    Middleware(app=None, db_url=db_url, engine_args={"poolclass": NullPool})

    status = _db.pool_status()
    assert status["pool_class"] == "NullPool"
    assert status["size"] is None
    assert status["capacity"] is None
    assert status["saturation"] is None


@pytest.mark.asyncio
async def test_pool_status_requires_initialised_middleware():
    _ensure_modules()
    from fastapi_async_sqlalchemy import create_middleware_and_session_proxy
    from fastapi_async_sqlalchemy.exceptions import SessionNotInitialisedError

    _, _db = create_middleware_and_session_proxy()

    with pytest.raises(SessionNotInitialisedError):
        _db.pool_status()


# ---------------------------------------------------------------------------
# pool_warn_threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_warning_logged_at_threshold(caplog):
    _ensure_modules()
    from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

    Middleware, _db = create_middleware_and_session_proxy()
    middleware = Middleware(
        app=None,
        db_url=db_url,
        engine_args={
            "poolclass": AsyncAdaptedQueuePool,
            "pool_size": 2,
            "max_overflow": 0,
        },
        pool_warn_threshold=0.9,
    )

    with caplog.at_level(logging.WARNING, logger="fastapi_async_sqlalchemy.middleware"):
        first = await middleware.engine.connect()
        assert not caplog.records, "50% saturation must stay quiet"
        second = await middleware.engine.connect()
        await first.close()
        await second.close()

    assert any("saturated" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_pool_warning_is_throttled(caplog):
    _ensure_modules()
    from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

    Middleware, _db = create_middleware_and_session_proxy()
    middleware = Middleware(
        app=None,
        db_url=db_url,
        engine_args={
            "poolclass": AsyncAdaptedQueuePool,
            "pool_size": 1,
            "max_overflow": 0,
        },
        pool_warn_threshold=0.9,
        pool_warn_interval=60,
    )

    with caplog.at_level(logging.WARNING, logger="fastapi_async_sqlalchemy.middleware"):
        for _ in range(5):
            connection = await middleware.engine.connect()
            await connection.close()

    assert len(caplog.records) == 1


@pytest.mark.asyncio
async def test_first_pool_warning_is_never_throttled(caplog):
    """The throttle must not swallow the very first warning.

    `time.monotonic()` counts from boot on Linux, so seeding the last-warning
    timestamp with 0.0 silenced freshly started containers for the length of
    the interval — exactly when saturation matters most.
    """
    _ensure_modules()
    from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

    Middleware, _db = create_middleware_and_session_proxy()
    middleware = Middleware(
        app=None,
        db_url=db_url,
        engine_args={
            "poolclass": AsyncAdaptedQueuePool,
            "pool_size": 1,
            "max_overflow": 0,
        },
        pool_warn_threshold=0.9,
        pool_warn_interval=86400,  # far larger than any plausible uptime
    )

    with caplog.at_level(logging.WARNING, logger="fastapi_async_sqlalchemy.middleware"):
        connection = await middleware.engine.connect()
        await connection.close()

    assert len(caplog.records) == 1


@pytest.mark.asyncio
async def test_pool_warning_removed_on_dispose(caplog):
    _ensure_modules()
    from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

    Middleware, _db = create_middleware_and_session_proxy()
    middleware = Middleware(
        app=None,
        db_url=db_url,
        engine_args={
            "poolclass": AsyncAdaptedQueuePool,
            "pool_size": 1,
            "max_overflow": 0,
        },
        pool_warn_threshold=0.9,
    )
    engine = middleware.engine
    await middleware.dispose()

    assert middleware._pool_warn_listener is None

    with caplog.at_level(logging.WARNING, logger="fastapi_async_sqlalchemy.middleware"):
        connection = await engine.connect()
        await connection.close()

    assert not caplog.records


@pytest.mark.asyncio
async def test_pool_warn_threshold_validated():
    _ensure_modules()
    from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

    Middleware, _db = create_middleware_and_session_proxy()

    with pytest.raises(ValueError, match="`pool_warn_threshold` must be within"):
        Middleware(app=None, db_url=db_url, pool_warn_threshold=1.5)


# ---------------------------------------------------------------------------
# exclude_paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_excluded_path_gets_no_request_session():
    _ensure_modules()
    from fastapi_async_sqlalchemy import create_middleware_and_session_proxy
    from fastapi_async_sqlalchemy.exceptions import MissingSessionError

    Middleware, _db = create_middleware_and_session_proxy()

    seen = {}

    async def downstream(scope, receive, send):
        try:
            seen["session"] = _db.session
        except MissingSessionError:
            seen["session"] = None
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = Middleware(app=downstream, db_url=db_url, exclude_paths=["/health"])

    async def receive():
        return {"type": "http.request"}

    async def send(_message):
        pass

    await middleware({"type": "http", "method": "GET", "path": "/health"}, receive, send)
    assert seen["session"] is None

    await middleware({"type": "http", "method": "GET", "path": "/items"}, receive, send)
    assert seen["session"] is not None


@pytest.mark.asyncio
async def test_excluded_path_still_serves_the_response():
    _ensure_modules()
    from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

    Middleware, _db = create_middleware_and_session_proxy()

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = Middleware(app=downstream, db_url=db_url, exclude_paths={"/health"})

    sent = []

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        sent.append(message)

    await middleware({"type": "http", "method": "GET", "path": "/health"}, receive, send)

    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[1]["body"] == b"ok"


# ---------------------------------------------------------------------------
# The incident scenario, end to end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_on_dedicated_engine_survives_saturated_business_pool():
    """The recommended fix: give the probe its own proxy and tiny engine.

    The business pool is fully saturated, yet the probe still answers — which
    is what keeps the pod in the load balancer's endpoints.
    """
    _ensure_modules()
    from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

    BusinessMiddleware, business_db = create_middleware_and_session_proxy()
    business = BusinessMiddleware(
        app=None,
        db_url=db_url,
        engine_args={
            "poolclass": AsyncAdaptedQueuePool,
            "pool_size": 1,
            "max_overflow": 0,
            "pool_timeout": 30,
        },
    )

    ProbeMiddleware, probe_db = create_middleware_and_session_proxy()
    ProbeMiddleware(
        app=None,
        db_url=db_url,
        engine_args={
            "poolclass": AsyncAdaptedQueuePool,
            "pool_size": 1,
            "max_overflow": 0,
            "pool_timeout": 1,
        },
    )

    saturate = await business.engine.connect()
    await saturate.execute(text("SELECT 1"))
    try:
        assert business_db.pool_status()["available"] == 0

        started = asyncio.get_running_loop().time()
        async with probe_db(pool_timeout=1):
            result = await probe_db.session.execute(text("SELECT 1"))
            assert result.scalar() == 1
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 1
    finally:
        await saturate.close()


# ---------------------------------------------------------------------------
# `pool_timeout` in multi-session mode must not be silently bypassed
#
# `db.session` is a synchronous property, so it cannot await a checkout and
# therefore cannot apply the context's deadline. It used to hand back a fresh
# session anyway, and the first query then parked on the engine-wide
# `pool_timeout` — the exact wait the caller asked to opt out of.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_session_is_rejected_under_multi_session_pool_timeout():
    middleware, _db = _make_pair()

    hold = await middleware.engine.connect()
    await hold.execute(text("SELECT 1"))
    try:
        async with _db(multi_sessions=True, pool_timeout=0.01):
            started = asyncio.get_running_loop().time()
            with pytest.raises(RuntimeError, match="db.connection|db.gather"):
                await _db.session.execute(text("SELECT 1"))
            elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 0.2, f"waited {elapsed}s — the engine pool_timeout won instead"
    finally:
        await hold.close()


@pytest.mark.asyncio
async def test_direct_session_still_works_without_a_context_pool_timeout():
    """The guard is scoped to `pool_timeout`; plain multi-session use is fine."""
    _, _db = _make_pair()

    async with _db(multi_sessions=True):
        result = await _db.session.execute(text("SELECT 5"))
        assert result.scalar() == 5


@pytest.mark.asyncio
async def test_session_created_by_connection_is_reusable_under_pool_timeout():
    """A session `db.connection()` already checked out stays reachable."""
    _, _db = _make_pair()

    async with _db(multi_sessions=True, pool_timeout=5):
        async with _db.connection() as session:
            assert _db.session is session
            result = await _db.session.execute(text("SELECT 6"))
            assert result.scalar() == 6
