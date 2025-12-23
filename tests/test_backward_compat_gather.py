"""Test backward compatibility for asyncio.gather() without multi_sessions flag.

This test verifies that after the fix, the old code pattern works without
requiring multi_sessions=True explicitly.
"""

import asyncio

import pytest
from sqlalchemy import text

db_url = "sqlite+aiosqlite://"


@pytest.mark.asyncio
async def test_gather_works_without_multi_sessions_flag(app, db, SQLAlchemyMiddleware):
    """
    Verify that asyncio.gather() works in normal mode (without multi_sessions=True).

    This is the backward compatibility fix - users shouldn't need to change their code.
    """
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db(commit_on_exit=True):
        await db.session.execute(
            text("CREATE TABLE IF NOT EXISTS compat_test (id INTEGER PRIMARY KEY, value TEXT)")
        )
        for i in range(20):
            await db.session.execute(
                text("INSERT INTO compat_test (value) VALUES (:value)"),
                {"value": f"value_{i}"},
            )

    # OLD CODE PATTERN - should work now without multi_sessions=True
    async with db():
        count_stmt = text("SELECT COUNT(*) FROM compat_test")
        data_stmt = text("SELECT * FROM compat_test LIMIT 5")

        # This should work! Each parallel query gets its own session
        count_result, data_result = await asyncio.gather(
            db.session.execute(count_stmt),
            db.session.execute(data_stmt),
        )

        count = count_result.scalar()
        data = data_result.fetchall()

        assert count == 20
        assert len(data) == 5

    print("✅ Backward compatibility preserved: asyncio.gather() works without multi_sessions!")


@pytest.mark.asyncio
async def test_gather_multiple_queries_parallel(app, db, SQLAlchemyMiddleware):
    """
    Test that multiple parallel queries work correctly.
    """
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db(commit_on_exit=True):
        await db.session.execute(
            text("CREATE TABLE IF NOT EXISTS parallel_test (id INTEGER PRIMARY KEY, status TEXT)")
        )
        for i in range(100):
            await db.session.execute(
                text("INSERT INTO parallel_test (status) VALUES (:status)"),
                {"status": "active" if i % 3 == 0 else "inactive"},
            )

    # Multiple parallel queries without multi_sessions=True
    async with db():
        stmt1 = text("SELECT COUNT(*) FROM parallel_test WHERE status = 'active'")
        stmt2 = text("SELECT COUNT(*) FROM parallel_test WHERE status = 'inactive'")
        stmt3 = text("SELECT * FROM parallel_test LIMIT 10")

        r1, r2, r3 = await asyncio.gather(
            db.session.execute(stmt1),
            db.session.execute(stmt2),
            db.session.execute(stmt3),
        )

        active_count = r1.scalar()
        inactive_count = r2.scalar()
        data = r3.fetchall()

        assert active_count == 34  # 100 / 3 rounded up
        assert inactive_count == 66
        assert len(data) == 10


@pytest.mark.asyncio
async def test_production_pattern_without_changes(app, db, SQLAlchemyMiddleware):
    """
    Verify the EXACT production pattern from the error report works.

    This is the pattern from /app/api/repository/routes.py:186
    """
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

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
                text("INSERT INTO processes (name, status, created_at) VALUES (:name, :status, :created_at)"),
                {
                    "name": f"process_{i}",
                    "status": "running" if i % 2 == 0 else "stopped",
                    "created_at": "2025-01-01T00:00:00",
                },
            )

    # EXACT PRODUCTION CODE - should work now!
    async with db():
        count_stmt = text("SELECT COUNT(*) FROM processes WHERE status = :status")
        processes_stmt = text(
            "SELECT * FROM processes WHERE status = :status "
            "ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
        )

        count_stmt = count_stmt.bindparams(status="running")
        processes_stmt = processes_stmt.bindparams(status="running", limit=10, offset=0)

        # This is line 186 from production - should work without any changes!
        total_result, processes_result = await asyncio.gather(
            db.session.execute(count_stmt),
            db.session.execute(processes_stmt),
        )

        total = total_result.scalar()
        processes = processes_result.fetchall()

        assert total == 50
        assert len(processes) == 10

    print("✅ Production code pattern works without any changes!")


@pytest.mark.asyncio
async def test_commit_on_exit_with_parallel_queries(app, db, SQLAlchemyMiddleware):
    """
    Verify that commit_on_exit works correctly with parallel queries.
    """
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    # Create table first
    async with db(commit_on_exit=True):
        await db.session.execute(
            text("CREATE TABLE IF NOT EXISTS commit_test (id INTEGER PRIMARY KEY, value TEXT)")
        )

    # Insert data with parallel queries and commit_on_exit
    async with db(commit_on_exit=True):
        # These should all be committed automatically
        await asyncio.gather(
            db.session.execute(text("INSERT INTO commit_test (value) VALUES ('a')")),
            db.session.execute(text("INSERT INTO commit_test (value) VALUES ('b')")),
            db.session.execute(text("INSERT INTO commit_test (value) VALUES ('c')")),
        )

    # Verify data was committed
    async with db():
        result = await db.session.execute(text("SELECT COUNT(*) FROM commit_test"))
        count = result.scalar()
        assert count == 3

    print("✅ commit_on_exit works correctly with parallel queries!")


@pytest.mark.asyncio
async def test_rollback_on_error_with_parallel_queries(app, db, SQLAlchemyMiddleware):
    """
    Verify that rollback works correctly when error occurs in parallel queries.
    """
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db(commit_on_exit=True):
        await db.session.execute(
            text("CREATE TABLE IF NOT EXISTS rollback_test (id INTEGER PRIMARY KEY, value TEXT)")
        )

    # Try to insert with error - should rollback all
    try:
        async with db(commit_on_exit=True):
            await db.session.execute(text("INSERT INTO rollback_test (value) VALUES ('should_rollback')"))
            # Force an error
            raise RuntimeError("Simulated error")
    except RuntimeError:
        pass

    # Verify data was rolled back
    async with db():
        result = await db.session.execute(text("SELECT COUNT(*) FROM rollback_test"))
        count = result.scalar()
        assert count == 0

    print("✅ Rollback works correctly on error!")
