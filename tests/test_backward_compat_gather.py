"""Supported alternatives to same-session ``asyncio.gather()`` patterns.

Concurrent operations on one SQLAlchemy ``AsyncSession`` are backend-dependent,
so these tests avoid treating that pattern as a library compatibility promise.
"""

import pytest
from sqlalchemy import text

db_url = "sqlite+aiosqlite://"


@pytest.mark.asyncio
async def test_sequential_queries_work_without_multi_sessions_flag(app, db, SQLAlchemyMiddleware):
    """
    Verify that normal single-session code can execute related queries sequentially.
    """
    SQLAlchemyMiddleware(app, db_url=db_url)

    async with db(commit_on_exit=True):
        await db.session.execute(
            text("CREATE TABLE IF NOT EXISTS compat_test (id INTEGER PRIMARY KEY, value TEXT)")
        )
        for i in range(20):
            await db.session.execute(
                text("INSERT INTO compat_test (value) VALUES (:value)"),
                {"value": f"value_{i}"},
            )

    async with db():
        count_stmt = text("SELECT COUNT(*) FROM compat_test")
        data_stmt = text("SELECT * FROM compat_test LIMIT 5")

        count_result = await db.session.execute(count_stmt)
        data_result = await db.session.execute(data_stmt)

        count = count_result.scalar()
        data = data_result.fetchall()

        assert count == 20
        assert len(data) == 5


@pytest.mark.asyncio
async def test_multiple_single_session_queries_run_sequentially(app, db, SQLAlchemyMiddleware):
    """
    Test that multiple related queries work correctly on one session.
    """
    SQLAlchemyMiddleware(app, db_url=db_url)

    async with db(commit_on_exit=True):
        await db.session.execute(
            text("CREATE TABLE IF NOT EXISTS parallel_test (id INTEGER PRIMARY KEY, status TEXT)")
        )
        for i in range(100):
            await db.session.execute(
                text("INSERT INTO parallel_test (status) VALUES (:status)"),
                {"status": "active" if i % 3 == 0 else "inactive"},
            )

    async with db():
        stmt1 = text("SELECT COUNT(*) FROM parallel_test WHERE status = 'active'")
        stmt2 = text("SELECT COUNT(*) FROM parallel_test WHERE status = 'inactive'")
        stmt3 = text("SELECT * FROM parallel_test LIMIT 10")

        r1 = await db.session.execute(stmt1)
        r2 = await db.session.execute(stmt2)
        r3 = await db.session.execute(stmt3)

        active_count = r1.scalar()
        inactive_count = r2.scalar()
        data = r3.fetchall()

        assert active_count == 34  # 100 / 3 rounded up
        assert inactive_count == 66
        assert len(data) == 10


@pytest.mark.asyncio
async def test_production_pattern_uses_sequential_queries(app, db, SQLAlchemyMiddleware):
    """
    Verify the production-style count and page queries with the supported pattern.
    """
    SQLAlchemyMiddleware(app, db_url=db_url)

    async with db(commit_on_exit=True):
        await db.session.execute(
            text("""
                CREATE TABLE IF NOT EXISTS processes (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT,
                    created_at TEXT
                )
            """)
        )
        for i in range(100):
            await db.session.execute(
                text(
                    """INSERT INTO
                           processes (name, status, created_at)
                       VALUES (:name, :status, :created_at)
                    """
                ),
                {
                    "name": f"process_{i}",
                    "status": "running" if i % 2 == 0 else "stopped",
                    "created_at": "2025-01-01T00:00:00",
                },
            )

    async with db():
        count_stmt = text("SELECT COUNT(*) FROM processes WHERE status = :status")
        processes_stmt = text(
            "SELECT * FROM processes WHERE status = :status "
            "ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
        )

        count_stmt = count_stmt.bindparams(status="running")
        processes_stmt = processes_stmt.bindparams(status="running", limit=10, offset=0)

        total_result = await db.session.execute(count_stmt)
        processes_result = await db.session.execute(processes_stmt)

        total = total_result.scalar()
        processes = processes_result.fetchall()

        assert total == 50
        assert len(processes) == 10


@pytest.mark.asyncio
async def test_commit_on_exit_with_sequential_writes(app, db, SQLAlchemyMiddleware):
    """
    Verify that commit_on_exit works correctly with sequential writes.
    """
    SQLAlchemyMiddleware(app, db_url=db_url)

    # Create table first
    async with db(commit_on_exit=True):
        await db.session.execute(
            text("CREATE TABLE IF NOT EXISTS commit_test (id INTEGER PRIMARY KEY, value TEXT)")
        )

    # Insert data with sequential writes and commit_on_exit.
    async with db(commit_on_exit=True):
        await db.session.execute(text("INSERT INTO commit_test (value) VALUES ('a')"))
        await db.session.execute(text("INSERT INTO commit_test (value) VALUES ('b')"))
        await db.session.execute(text("INSERT INTO commit_test (value) VALUES ('c')"))

    # Verify data was committed
    async with db():
        result = await db.session.execute(text("SELECT COUNT(*) FROM commit_test"))
        count = result.scalar()
        assert count == 3


@pytest.mark.asyncio
async def test_rollback_on_error_with_sequential_queries(app, db, SQLAlchemyMiddleware):
    """
    Verify that rollback works correctly when an error occurs.
    """
    SQLAlchemyMiddleware(app, db_url=db_url)

    async with db(commit_on_exit=True):
        await db.session.execute(
            text("CREATE TABLE IF NOT EXISTS rollback_test (id INTEGER PRIMARY KEY, value TEXT)")
        )

    # Try to insert with error - should rollback all
    try:
        async with db(commit_on_exit=True):
            await db.session.execute(
                text("INSERT INTO rollback_test (value) VALUES ('should_rollback')")
            )
            # Force an error
            raise RuntimeError("Simulated error")
    except RuntimeError:
        pass

    # Verify data was rolled back
    async with db():
        result = await db.session.execute(text("SELECT COUNT(*) FROM rollback_test"))
        count = result.scalar()
        assert count == 0
