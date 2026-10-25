"""Regressions for lifecycle, commit and throttling defects found in review."""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, PendingRollbackError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import AsyncAdaptedQueuePool, StaticPool

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
            strict=True,
        )
    }
    return closure[var_name]


def _queue_pool_engine(tmp_path):
    # A file database behind a real queue pool, so `checkedout()` is tracked.
    return create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'review.db'}",
        poolclass=AsyncAdaptedQueuePool,
        pool_size=5,
    )


def test_app_survives_a_second_lifespan_cycle():
    """Shutdown must not leave `db` unbound for the next startup of the same app."""
    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url=DB_URL)

    @app.get("/")
    async def read():
        return (await db.session.execute(text("SELECT 1"))).scalar()

    for _ in range(2):
        with TestClient(app) as client:
            response = client.get("/")
            assert response.status_code == 200
            assert response.json() == 1


def test_second_lifespan_cycle_restores_the_pool_warning(caplog):
    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    engine = create_async_engine(DB_URL, poolclass=AsyncAdaptedQueuePool, pool_size=1)
    app = FastAPI()
    app.add_middleware(
        SQLAlchemyMiddleware, custom_engine=engine, pool_warn_threshold=0.01, pool_warn_interval=0
    )

    @app.get("/")
    async def read():
        return (await db.session.execute(text("SELECT 1"))).scalar()

    for _ in range(2):
        caplog.clear()
        with TestClient(app) as client:
            assert client.get("/").status_code == 200
        assert any("saturated" in record.getMessage() for record in caplog.records)


def test_lifespan_startup_fails_when_proxy_was_rebound_meanwhile():
    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url=DB_URL)

    with TestClient(app):
        pass

    other = SQLAlchemyMiddleware(FastAPI(), db_url=DB_URL)
    try:
        with pytest.raises(RuntimeError, match="bound to another engine"):
            with TestClient(app):
                pass
    finally:
        asyncio.run(other.dispose())


class _Base(DeclarativeBase):
    pass


class _Item(_Base):
    __tablename__ = "item"
    id: Mapped[int] = mapped_column(primary_key=True)


async def _add_then_swallow_a_failed_flush(session):
    session.add(_Item(id=10))
    await session.flush()
    session.add(_Item(id=1))
    session.add(_Item(id=1))
    try:
        await session.flush()
    except IntegrityError:
        pass


@pytest.mark.asyncio
async def test_commit_on_exit_surfaces_a_swallowed_flush_failure():
    """A deactivated transaction must not be reported as a successful commit."""
    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    engine = create_async_engine(DB_URL, poolclass=StaticPool)
    SQLAlchemyMiddleware(FastAPI(), custom_engine=engine)

    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)

    with pytest.raises(PendingRollbackError):
        async with db(commit_on_exit=True):
            await _add_then_swallow_a_failed_flush(db.session)

    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM item"))).scalar() == 0

    await engine.dispose()


def test_commit_on_exit_turns_a_swallowed_orm_flush_failure_into_500():
    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    engine = create_async_engine(DB_URL, poolclass=StaticPool)
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, custom_engine=engine, commit_on_exit=True)

    @app.post("/")
    async def write():
        await _add_then_swallow_a_failed_flush(db.session)
        return "ok"

    async def create_schema():
        async with engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    asyncio.run(create_schema())

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post("/").status_code == 500


@pytest.mark.asyncio
async def test_cancelled_multi_session_exit_returns_connections(tmp_path):
    """Cancelling the parent while it waits for child tasks must not leak sessions."""
    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    engine = _queue_pool_engine(tmp_path)
    SQLAlchemyMiddleware(FastAPI(), custom_engine=engine)
    multi_state = _get_ctx_var(db, "_multi_state")
    child_started = asyncio.Event()

    async def slow_to_cancel_child():
        await db.session.execute(text("SELECT 1"))
        child_started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)
            raise

    async def parent():
        async with db(multi_sessions=True):
            await db.session.execute(text("SELECT 1"))
            asyncio.create_task(slow_to_cancel_child())
            await child_started.wait()
        pytest.fail("unreachable")  # pragma: no cover

    parent_task = asyncio.create_task(parent())
    await child_started.wait()
    await asyncio.sleep(0.05)  # parent is now inside __aexit__, awaiting the child
    parent_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent_task

    await asyncio.sleep(0.3)  # let the child finish its own cancellation
    assert engine.pool.checkedout() == 0
    assert multi_state.get() is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_driver_timeout_is_not_reported_as_pool_exhaustion(monkeypatch):
    from fastapi_async_sqlalchemy import PoolTimeoutError

    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    middleware = SQLAlchemyMiddleware(FastAPI(), db_url=DB_URL)

    async def connect_times_out(self, *args, **kwargs):
        raise TimeoutError("server did not answer the connect")

    monkeypatch.setattr(AsyncSession, "connection", connect_times_out)

    with pytest.raises(TimeoutError, match="server did not answer") as exc_info:
        async with db(pool_timeout=5):
            pass
    assert not isinstance(exc_info.value, PoolTimeoutError)

    await middleware.dispose()


@pytest.mark.asyncio
async def test_engine_timeout_reports_the_engine_deadline():
    from fastapi_async_sqlalchemy import PoolTimeoutError

    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    engine = create_async_engine(
        DB_URL, poolclass=AsyncAdaptedQueuePool, pool_size=1, max_overflow=0, pool_timeout=0.05
    )
    SQLAlchemyMiddleware(FastAPI(), custom_engine=engine)

    hold = await engine.connect()
    await hold.execute(text("SELECT 1"))
    try:
        with pytest.raises(PoolTimeoutError) as exc_info:
            async with db(pool_timeout=10):
                pass
    finally:
        await hold.close()

    assert exc_info.value.timeout == 0.05
    await engine.dispose()


@pytest.mark.asyncio
async def test_reused_connection_context_releases_its_slot_once():
    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    middleware = SQLAlchemyMiddleware(FastAPI(), db_url=DB_URL)
    multi_state = _get_ctx_var(db, "_multi_state")

    async with db(multi_sessions=True, max_concurrent=1):

        async def use_twice():
            ctx = db.connection()
            async with ctx as session:
                await session.execute(text("SELECT 1"))
            # Second entry borrows the task's session and takes no slot, so it
            # must not give back the one the first entry already returned.
            async with db.connection():
                async with ctx as session:
                    await session.execute(text("SELECT 1"))

        await asyncio.create_task(use_twice())
        assert multi_state.get().semaphore._value == 1

    await middleware.dispose()


@pytest.mark.asyncio
async def test_reused_db_context_closes_every_session(tmp_path):
    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    engine = _queue_pool_engine(tmp_path)
    SQLAlchemyMiddleware(FastAPI(), custom_engine=engine)

    ctx = db()
    for _ in range(2):
        async with ctx:
            await db.session.execute(text("SELECT 1"))
        assert engine.pool.checkedout() == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_reused_db_context_nested_in_multi_then_alone():
    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    middleware = SQLAlchemyMiddleware(FastAPI(), db_url=DB_URL)

    ctx = db()
    async with db(multi_sessions=True):
        async with ctx:
            await db.session.execute(text("SELECT 1"))
    async with ctx:
        await db.session.execute(text("SELECT 1"))

    await middleware.dispose()


@pytest.mark.asyncio
async def test_gather_refuses_to_deadlock_on_its_own_slot():
    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    middleware = SQLAlchemyMiddleware(FastAPI(), db_url=DB_URL)

    async def work(n):
        return (await db.session.execute(text(f"SELECT {n}"))).scalar()

    async with db(multi_sessions=True, max_concurrent=1):

        async def holder():
            async with db.connection():
                coro = work(1)
                with pytest.raises(RuntimeError, match="would deadlock"):
                    await asyncio.wait_for(db.gather(coro), timeout=1)
                assert coro.cr_frame is None  # closed, not left unawaited

        await asyncio.create_task(holder())

    await middleware.dispose()


@pytest.mark.asyncio
async def test_gather_from_a_slot_holder_still_works_with_spare_slots():
    SQLAlchemyMiddleware, db = _make_middleware_and_db()
    middleware = SQLAlchemyMiddleware(FastAPI(), db_url=DB_URL)

    async def work(n):
        return (await db.session.execute(text(f"SELECT {n}"))).scalar()

    async with db(multi_sessions=True, max_concurrent=2):

        async def holder():
            async with db.connection():
                return await db.gather(work(1), work(2), work(3))

        assert await asyncio.create_task(holder()) == [1, 2, 3]

    await middleware.dispose()


def test_admission_semaphore_works_across_event_loops():
    SQLAlchemyMiddleware, _db = _make_middleware_and_db()
    middleware = SQLAlchemyMiddleware(FastAPI(), db_url=DB_URL, max_concurrent_requests=1)

    async def contend():
        semaphore = middleware._admission_semaphore()
        await semaphore.acquire()
        waiter = asyncio.create_task(semaphore.acquire())
        await asyncio.sleep(0)  # the waiter binds the semaphore to this loop
        semaphore.release()
        await waiter
        semaphore.release()

    asyncio.run(contend())
    asyncio.run(contend())
    asyncio.run(middleware.dispose())


def test_failed_sessionmaker_does_not_bind_the_proxy(monkeypatch):
    import fastapi_async_sqlalchemy.middleware as middleware_module

    SQLAlchemyMiddleware, db = _make_middleware_and_db()

    def broken_sessionmaker(*args, **kwargs):
        raise TypeError("bad session_args")

    monkeypatch.setattr(middleware_module, "async_sessionmaker", broken_sessionmaker)
    with pytest.raises(TypeError, match="bad session_args"):
        SQLAlchemyMiddleware(FastAPI(), db_url=DB_URL)
    monkeypatch.undo()

    # The proxy is still free, so a corrected init succeeds.
    middleware = SQLAlchemyMiddleware(FastAPI(), db_url=DB_URL)
    asyncio.run(middleware.dispose())


@pytest.mark.parametrize("value", [True, 2.5, "3"])
def test_max_concurrent_must_be_an_integer(value):
    _SQLAlchemyMiddleware, db = _make_middleware_and_db()
    with pytest.raises(ValueError, match="`max_concurrent` must be greater than 0"):
        db(multi_sessions=True, max_concurrent=value)
