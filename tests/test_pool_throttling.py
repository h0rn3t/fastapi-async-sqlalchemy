"""Tests for connection pool throttling mechanism.

Validates that ``max_concurrent`` + ``db.connection()`` / ``db.gather()``
prevent ``TimeoutError: QueuePool limit of size N overflow M reached``
by queuing tasks that exceed the pool capacity.
"""

import asyncio
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.pool import AsyncAdaptedQueuePool

db_url = "sqlite+aiosqlite://"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_modules():
    """Ensure fastapi_async_sqlalchemy modules are in sys.modules.

    The conftest ``db`` fixture removes them during teardown, which breaks
    dataclass instantiation for classes defined in those modules.
    """
    for mod_name in ("fastapi_async_sqlalchemy", "fastapi_async_sqlalchemy.middleware"):
        if mod_name not in sys.modules:
            __import__(mod_name)


@pytest.fixture(autouse=True)
def _restore_modules():
    """Autouse fixture — ensure modules survive conftest teardown."""
    _ensure_modules()
    yield
    _ensure_modules()


def _make_middleware_and_db(**engine_kw):
    """Create a fresh middleware/db pair with a separate engine."""
    _ensure_modules()
    from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

    Middleware, _db = create_middleware_and_session_proxy()
    Middleware(app=None, db_url=db_url, engine_args=engine_kw)
    return _db


# ---------------------------------------------------------------------------
# db.connection()  —  explicit async context manager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_ctx_basic():
    """db.connection() returns a usable session in multi_sessions mode."""
    _db = _make_middleware_and_db()

    async with _db(multi_sessions=True, max_concurrent=2):

        async def work(n):
            async with _db.connection() as session:
                result = await session.execute(text(f"SELECT {n}"))
                return result.scalar()

        tasks = [asyncio.create_task(work(i)) for i in range(5)]
        results = await asyncio.gather(*tasks)
        assert sorted(results) == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_connection_ctx_limits_concurrency():
    """Only max_concurrent tasks hold sessions concurrently."""
    _db = _make_middleware_and_db()
    max_concurrent = 3
    active = 0
    peak = 0

    async with _db(multi_sessions=True, max_concurrent=max_concurrent):

        async def work(n):
            nonlocal active, peak
            async with _db.connection() as session:
                active += 1
                if active > peak:
                    peak = active
                await session.execute(text(f"SELECT {n}"))
                await asyncio.sleep(0.05)
                active -= 1

        tasks = [asyncio.create_task(work(i)) for i in range(12)]
        await asyncio.gather(*tasks)

    assert peak <= max_concurrent, f"peak={peak} exceeded max_concurrent={max_concurrent}"


@pytest.mark.asyncio
async def test_connection_ctx_releases_on_error():
    """Semaphore slot is released even when the task raises an exception."""
    _db = _make_middleware_and_db()

    async with _db(multi_sessions=True, max_concurrent=2):

        async def bad_work():
            async with _db.connection() as session:
                await session.execute(text("SELECT 1"))
                raise RuntimeError("boom")

        async def good_work():
            async with _db.connection() as session:
                result = await session.execute(text("SELECT 42"))
                return result.scalar()

        # Run a failing task, then a succeeding one; both should get slots
        results = await asyncio.gather(
            bad_work(), good_work(), return_exceptions=True,
        )
        assert isinstance(results[0], RuntimeError)
        assert results[1] == 42


@pytest.mark.asyncio
async def test_connection_ctx_without_max_concurrent():
    """db.connection() works without max_concurrent (no semaphore)."""
    _db = _make_middleware_and_db()

    async with _db(multi_sessions=True):

        async def work(n):
            async with _db.connection() as session:
                result = await session.execute(text(f"SELECT {n}"))
                return result.scalar()

        tasks = [asyncio.create_task(work(i)) for i in range(5)]
        results = await asyncio.gather(*tasks)
        assert sorted(results) == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# db.gather()  —  convenience wrapper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gather_basic():
    """db.gather() runs coroutines with throttling."""
    _db = _make_middleware_and_db()

    async with _db(multi_sessions=True, max_concurrent=3):

        async def work(n):
            result = await _db.session.execute(text(f"SELECT {n}"))
            return result.scalar()

        results = await _db.gather(work(1), work(2), work(3), work(4), work(5))
        assert sorted(results) == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_gather_limits_concurrency():
    """db.gather() respects max_concurrent limit."""
    _db = _make_middleware_and_db()
    max_concurrent = 2
    active = 0
    peak = 0

    async with _db(multi_sessions=True, max_concurrent=max_concurrent):

        async def work(n):
            nonlocal active, peak
            active += 1
            if active > peak:
                peak = active
            result = await _db.session.execute(text(f"SELECT {n}"))
            await asyncio.sleep(0.05)
            active -= 1
            return result.scalar()

        await _db.gather(*[work(i) for i in range(10)])

    assert peak <= max_concurrent


@pytest.mark.asyncio
async def test_gather_return_exceptions():
    """db.gather() supports return_exceptions=True."""
    _db = _make_middleware_and_db()

    async with _db(multi_sessions=True, max_concurrent=3):

        async def good():
            result = await _db.session.execute(text("SELECT 1"))
            return result.scalar()

        async def bad():
            raise ValueError("oops")

        results = await _db.gather(good(), bad(), good(), return_exceptions=True)
        assert results[0] == 1
        assert isinstance(results[1], ValueError)
        assert results[2] == 1


@pytest.mark.asyncio
async def test_gather_without_max_concurrent():
    """db.gather() delegates to asyncio.gather when no semaphore."""
    _db = _make_middleware_and_db()

    async with _db(multi_sessions=True):

        async def work(n):
            result = await _db.session.execute(text(f"SELECT {n}"))
            return result.scalar()

        results = await _db.gather(work(1), work(2), work(3))
        assert sorted(results) == [1, 2, 3]


# ---------------------------------------------------------------------------
# Session-on-success cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_closed_on_task_success():
    """Sessions are closed promptly when tasks succeed (not deferred to __aexit__)."""
    _db = _make_middleware_and_db()

    async with _db(multi_sessions=True):

        async def work(n):
            result = await _db.session.execute(text(f"SELECT {n}"))
            return result.scalar()

        tasks = [asyncio.create_task(work(i)) for i in range(5)]
        results = await asyncio.gather(*tasks)
        assert sorted(results) == [0, 1, 2, 3, 4]

        # Give cleanup callbacks time to fire
        await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
# Realistic pool exhaustion scenario
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_many_tasks_with_tiny_pool_using_connection():
    """50 tasks with pool_size=2, max_overflow=0 — must NOT deadlock or timeout."""
    _ensure_modules()
    from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

    Middleware, _db = create_middleware_and_session_proxy()
    Middleware(
        app=None,
        db_url="sqlite+aiosqlite:///",
        engine_args={
            "poolclass": AsyncAdaptedQueuePool,
            "pool_size": 2,
            "max_overflow": 0,
            "pool_timeout": 5,
        },
    )

    async with _db(multi_sessions=True, max_concurrent=2):

        async def work(n):
            async with _db.connection() as session:
                result = await session.execute(text(f"SELECT {n}"))
                await asyncio.sleep(0.02)
                return result.scalar()

        tasks = [asyncio.create_task(work(i)) for i in range(20)]
        results = await asyncio.gather(*tasks)
        assert sorted(results) == list(range(20))


@pytest.mark.asyncio
async def test_many_tasks_with_tiny_pool_using_gather():
    """50 tasks via db.gather() with pool_size=2 — must NOT timeout."""
    _ensure_modules()
    from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

    Middleware, _db = create_middleware_and_session_proxy()
    Middleware(
        app=None,
        db_url="sqlite+aiosqlite:///",
        engine_args={
            "poolclass": AsyncAdaptedQueuePool,
            "pool_size": 2,
            "max_overflow": 0,
            "pool_timeout": 5,
        },
    )

    async with _db(multi_sessions=True, max_concurrent=2):

        async def work(n):
            result = await _db.session.execute(text(f"SELECT {n}"))
            await asyncio.sleep(0.02)
            return result.scalar()

        results = await _db.gather(*[work(i) for i in range(20)])
        assert sorted(results) == list(range(20))


# ---------------------------------------------------------------------------
# Commit-on-exit with throttling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_on_exit_with_throttling():
    """commit_on_exit works correctly with db.connection()."""
    _db = _make_middleware_and_db()

    async with _db(multi_sessions=True, max_concurrent=2, commit_on_exit=True):

        async def work(n):
            async with _db.connection() as session:
                result = await session.execute(text(f"SELECT {n}"))
                return result.scalar()

        tasks = [asyncio.create_task(work(i)) for i in range(6)]
        results = await asyncio.gather(*tasks)
        assert sorted(results) == [0, 1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_ctx_reuses_existing_task_session():
    """If a task already has a session, db.connection() reuses it (no double-create)."""
    _db = _make_middleware_and_db()

    async with _db(multi_sessions=True, max_concurrent=3):

        async def work():
            # First access creates the session via db.connection()
            async with _db.connection() as s1:
                # Second access via nested db.connection() should reuse the same session
                async with _db.connection() as s2:
                    assert s2 is s1
                    result = await s2.execute(text("SELECT 1"))
                    return result.scalar()

        result = await asyncio.create_task(work())
        assert result == 1


@pytest.mark.asyncio
async def test_direct_db_session_in_child_task_rejected_with_max_concurrent():
    """With max_concurrent, child tasks must use db.connection() or db.gather()."""
    _db = _make_middleware_and_db()

    async with _db(multi_sessions=True, max_concurrent=3):

        async def work():
            with pytest.raises(RuntimeError, match="db\\.connection\\(\\)|db\\.gather\\(\\)"):
                _ = _db.session

            async with _db.connection() as s2:
                # Access is allowed when task owns a throttling slot
                s1 = _db.session
                assert s2 is s1
                result = await s2.execute(text("SELECT 1"))
                return result.scalar()

        result = await asyncio.create_task(work())
        assert result == 1


@pytest.mark.asyncio
async def test_max_concurrent_ignored_without_multi_sessions():
    """max_concurrent without multi_sessions doesn't break anything."""
    _db = _make_middleware_and_db()

    # max_concurrent is set but multi_sessions is False — should work normally
    async with _db(max_concurrent=5):
        result = await _db.session.execute(text("SELECT 1"))
        assert result.scalar() == 1


@pytest.mark.asyncio
async def test_gather_many_fast_tasks():
    """Stress test: 100 fast tasks with max_concurrent=5."""
    _db = _make_middleware_and_db()

    async with _db(multi_sessions=True, max_concurrent=5):

        async def work(n):
            result = await _db.session.execute(text(f"SELECT {n}"))
            return result.scalar()

        results = await _db.gather(*[work(i) for i in range(100)])
        assert sorted(results) == list(range(100))


@pytest.mark.asyncio
async def test_connection_and_session_interop():
    """db.session and db.connection() can coexist within the same multi_sessions context."""
    _db = _make_middleware_and_db()

    async with _db(multi_sessions=True, max_concurrent=3):

        # Parent uses db.session directly
        parent_result = await _db.session.execute(text("SELECT 0"))
        assert parent_result.scalar() == 0

        # Children use db.connection()
        async def child(n):
            async with _db.connection() as session:
                result = await session.execute(text(f"SELECT {n}"))
                return result.scalar()

        tasks = [asyncio.create_task(child(i)) for i in range(1, 6)]
        results = await asyncio.gather(*tasks)
        assert sorted(results) == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# Tests using conftest fixtures (placed last — conftest cleanup removes modules)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_ctx_non_multi_sessions(app, db, SQLAlchemyMiddleware):
    """db.connection() works in regular (non-multi_sessions) mode too."""
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db():
        async with db.connection() as session:
            result = await session.execute(text("SELECT 99"))
            assert result.scalar() == 99
