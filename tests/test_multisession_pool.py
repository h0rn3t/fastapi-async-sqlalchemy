import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.pool import AsyncAdaptedQueuePool

from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

"""
Goal: Ensure that session for each task is closed immediately after task completion
to prevent session accumulation and connection pool exhaustion.
"""

# Create separate middleware for testing
TestSQLAlchemyMiddleware, test_db = create_middleware_and_session_proxy()


async def execute_query(query_id: int):
    """Execute query using session"""
    result = await test_db.session.execute(text(f"SELECT {query_id} as id"))
    # Simulate a long operation
    await asyncio.sleep(0.5)
    return result.fetchone()


@pytest.mark.asyncio
async def test_multisession_with_limited_pool():
    """Test: 20 coroutines with multisession=True with a pool of 5 connections"""

    TestSQLAlchemyMiddleware(
        app=None,
        db_url="sqlite+aiosqlite:///test.db",
        engine_args={
            "poolclass": AsyncAdaptedQueuePool,
            "pool_size": 5,
            "max_overflow": 0,
            "echo": False,
        },
    )

    async with test_db(multi_sessions=True):
        # Create 20 coroutines
        tasks = [asyncio.create_task(execute_query(i)) for i in range(20)]

        # Execute all tasks in parallel
        results = await asyncio.gather(*tasks)

        # Checks
        assert len(results) == 20
        assert all(result is not None for result in results)


@pytest.mark.asyncio
async def test_different_tasks_get_different_sessions():
    """Test: different tasks get different sessions, same task gets same session"""

    TestSQLAlchemyMiddleware(
        app=None,
        db_url="sqlite+aiosqlite:///:memory:",
    )

    session_ids = []

    async with test_db(multi_sessions=True):

        async def worker():
            s1 = test_db.session
            s2 = test_db.session
            # Same task should get same session
            assert id(s1) == id(s2), "Same task should get same session"
            session_ids.append(id(s1))
            await s1.execute(text("SELECT 1"))

        tasks = [asyncio.create_task(worker()) for _ in range(5)]
        await asyncio.gather(*tasks)

        # Different tasks should get different sessions
        assert len(set(session_ids)) == 5, "Different tasks should get different sessions"


if __name__ == "__main__":
    asyncio.run(test_multisession_with_limited_pool())
