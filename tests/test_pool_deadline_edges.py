import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
@pytest.mark.parametrize("pool_size,max_overflow", [(2, -1), (0, 10)])
async def test_unlimited_pool_does_not_report_finite_capacity(tmp_path, pool_size, max_overflow):
    from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'unlimited.db'}",
        pool_size=pool_size,
        max_overflow=max_overflow,
    )
    middleware_class, db = create_middleware_and_session_proxy()
    middleware_class(None, custom_engine=engine)
    try:
        status = db.pool_status()
        assert status["capacity"] is None
        assert status["available"] is None
        assert status["saturation"] is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_parent_shutdown_cancels_pool_checkout_after_semaphore_acquired(tmp_path):
    from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'shutdown.db'}",
        pool_size=1,
        max_overflow=0,
    )
    middleware_class, db = create_middleware_and_session_proxy()
    middleware_class(None, custom_engine=engine)
    holding = await engine.connect()
    started = asyncio.Event()

    async def waiter():
        started.set()
        async with db.connection(timeout=10):
            pytest.fail("checkout must not complete after parent shutdown")

    task = None
    try:
        async with db(multi_sessions=True, max_concurrent=1):
            task = asyncio.create_task(waiter())
            await started.wait()
        assert task.cancelled()
        assert engine.pool.checkedout() == 1
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await holding.close()
        await engine.dispose()


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_checkout_deadlines_must_be_finite(value):
    from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

    _, db = create_middleware_and_session_proxy()
    with pytest.raises(ValueError):
        db(pool_timeout=value)
    with pytest.raises(ValueError):
        db.connection(timeout=value)


@pytest.mark.asyncio
async def test_connection_deadline_includes_semaphore_wait(tmp_path):
    from fastapi_async_sqlalchemy import PoolTimeoutError, create_middleware_and_session_proxy

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'wait.db'}")
    middleware_class, db = create_middleware_and_session_proxy()
    middleware_class(None, custom_engine=engine)
    holding = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with db.connection():
            holding.set()
            await release.wait()

    async def waiter():
        try:
            async with db.connection(timeout=0.01):
                return None
        except PoolTimeoutError as exc:
            return exc

    try:
        async with db(multi_sessions=True, max_concurrent=1):
            first = asyncio.create_task(holder())
            await holding.wait()
            second = asyncio.create_task(waiter())
            try:
                done, _ = await asyncio.wait([second], timeout=0.3)
                assert second in done, "checkout deadline did not cover the semaphore queue"
                assert isinstance(second.result(), PoolTimeoutError)
            finally:
                release.set()
                await asyncio.gather(first, second)
    finally:
        await engine.dispose()
