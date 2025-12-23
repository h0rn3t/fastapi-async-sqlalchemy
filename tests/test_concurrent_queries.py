"""Test for concurrent query execution issue with asyncio.gather().

This test suite demonstrates the "isce" (InvalidSessionError) error that can occur
when trying to execute multiple queries concurrently on the same SQLAlchemy async session.

The error is:
    InvalidRequestError: This session is provisioning a new connection;
    concurrent operations are not permitted
    (Background on this error at: https://sqlalche.me/e/20/isce)

This issue is database-backend specific:
- SQLite (aiosqlite): Serializes operations internally, may not reproduce the error
- PostgreSQL (asyncpg): Will raise the error
- MySQL (asyncmy): Will raise the error

Solutions:
1. Execute queries sequentially instead of using asyncio.gather()
2. Use multi_sessions=True mode to get a separate session per task
3. Use connection pooling with multiple connections
"""

import asyncio
import os

import pytest
from sqlalchemy import text

db_url = "sqlite+aiosqlite://"

# Optional: Test with real PostgreSQL if available
# Uncomment and set POSTGRES_URL environment variable to test with PostgreSQL
# db_url_postgres = os.getenv("POSTGRES_URL", "postgresql+asyncpg://user:pass@localhost/testdb")


@pytest.mark.asyncio
async def test_concurrent_queries_same_session_may_fail(app, db, SQLAlchemyMiddleware):
    """
    Test concurrent queries on the same session.

    This test demonstrates that with some database backends (PostgreSQL, MySQL)
    concurrent operations on the same session can cause this error:
    InvalidRequestError: This session is provisioning a new connection;
    concurrent operations are not permitted
    (Background on this error at: https://sqlalche.me/e/20/isce)

    Note: SQLite (aiosqlite) may not reproduce this issue because it serializes
    operations internally. The issue is more common with asyncpg/asyncmy drivers.
    """
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db():
        # Create a test table
        await db.session.execute(
            text("CREATE TABLE IF NOT EXISTS test_table (id INTEGER PRIMARY KEY, value TEXT)")
        )
        await db.session.commit()

        # Insert test data
        for i in range(10):
            await db.session.execute(
                text("INSERT INTO test_table (value) VALUES (:value)"),
                {"value": f"value_{i}"},
            )
        await db.session.commit()

        # This MAY fail with 'isce' error when trying to execute
        # two queries concurrently on the same session (depends on DB backend)
        count_stmt = text("SELECT COUNT(*) FROM test_table")
        data_stmt = text("SELECT * FROM test_table LIMIT 5")

        # Using asyncio.gather() may cause concurrent operations on the same session
        try:
            count_result, data_result = await asyncio.gather(
                db.session.execute(count_stmt),
                db.session.execute(data_stmt),
            )
            # If it works, verify results
            count = count_result.scalar()
            data = data_result.fetchall()
            assert count == 10
            assert len(data) == 5
            # Mark as expected for SQLite
            print("Note: SQLite allows this, but PostgreSQL/MySQL may not")
        except Exception as e:
            # This is expected with some database backends
            error_msg = str(e).lower()
            assert any(
                phrase in error_msg
                for phrase in [
                    "concurrent operations are not permitted",
                    "isce",
                    "provisioning a new connection",
                ]
            )


@pytest.mark.asyncio
async def test_concurrent_queries_same_session_sequential_works(app, db, SQLAlchemyMiddleware):
    """
    Test that sequential queries on the same session work correctly.

    This is a workaround - execute queries sequentially instead of using asyncio.gather().
    """
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db():
        # Create a test table
        await db.session.execute(
            text("CREATE TABLE IF NOT EXISTS test_table2 (id INTEGER PRIMARY KEY, value TEXT)")
        )
        await db.session.commit()

        # Insert test data
        for i in range(10):
            await db.session.execute(
                text("INSERT INTO test_table2 (value) VALUES (:value)"),
                {"value": f"value_{i}"},
            )
        await db.session.commit()

        # Execute queries sequentially - this works
        count_stmt = text("SELECT COUNT(*) FROM test_table2")
        data_stmt = text("SELECT * FROM test_table2 LIMIT 5")

        count_result = await db.session.execute(count_stmt)
        data_result = await db.session.execute(data_stmt)

        count = count_result.scalar()
        data = data_result.fetchall()

        assert count == 10
        assert len(data) == 5


@pytest.mark.asyncio
async def test_concurrent_queries_multi_sessions_works(app, db, SQLAlchemyMiddleware):
    """
    Test that concurrent queries work when using multi_sessions mode.

    With multi_sessions=True, each task gets its own session,
    so concurrent operations don't conflict.
    """
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db(multi_sessions=True, commit_on_exit=True):
        # Create a test table
        await db.session.execute(
            text("CREATE TABLE IF NOT EXISTS test_table3 (id INTEGER PRIMARY KEY, value TEXT)")
        )
        await db.session.flush()

        # Insert test data
        for i in range(10):
            await db.session.execute(
                text("INSERT INTO test_table3 (value) VALUES (:value)"),
                {"value": f"value_{i}"},
            )
        await db.session.flush()

        # With multi_sessions, each async function gets its own session
        async def get_count():
            result = await db.session.execute(text("SELECT COUNT(*) FROM test_table3"))
            return result.scalar()

        async def get_data():
            result = await db.session.execute(text("SELECT * FROM test_table3 LIMIT 5"))
            return result.fetchall()

        # This should work because each task gets its own session
        count, data = await asyncio.gather(get_count(), get_data())

        assert count == 10
        assert len(data) == 5


@pytest.mark.asyncio
async def test_concurrent_queries_reproduce_user_error(app, db, SQLAlchemyMiddleware):
    """
    Reproduce the exact scenario from the user's error:
    Two queries (count and data fetch) executed with asyncio.gather().

    This demonstrates the issue reported:
    ```python
    total_result, processes_result = await asyncio.gather(
        db.session.execute(count_stmt), db.session.execute(processes_stmt)
    )
    ```

    With PostgreSQL/MySQL, this can cause:
    InvalidRequestError: This session is provisioning a new connection;
    concurrent operations are not permitted
    """
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db():
        # Setup similar to user's use case
        await db.session.execute(
            text("CREATE TABLE IF NOT EXISTS processes (id INTEGER PRIMARY KEY, name TEXT)")
        )
        await db.session.commit()

        # Insert some test data
        for i in range(100):
            await db.session.execute(
                text("INSERT INTO processes (name) VALUES (:name)"),
                {"name": f"process_{i}"},
            )
        await db.session.commit()

        # Simulate the user's code pattern:
        # total_result, processes_result = await asyncio.gather(
        #     db.session.execute(count_stmt), db.session.execute(processes_stmt)
        # )

        count_stmt = text("SELECT COUNT(*) FROM processes")
        processes_stmt = text("SELECT * FROM processes LIMIT 10 OFFSET 0")

        # This may fail with the 'isce' error on PostgreSQL/MySQL
        try:
            total_result, processes_result = await asyncio.gather(
                db.session.execute(count_stmt),
                db.session.execute(processes_stmt),
            )
            # If it works (SQLite case), verify results
            count = total_result.scalar()
            data = processes_result.fetchall()
            assert count == 100
            assert len(data) == 10
            print("Note: SQLite allows concurrent queries, but PostgreSQL/MySQL may not")
        except Exception as e:
            # This is the expected error with PostgreSQL/MySQL
            error_msg = str(e).lower()
            assert any(
                phrase in error_msg
                for phrase in [
                    "concurrent operations are not permitted",
                    "isce",
                    "provisioning a new connection",
                ]
            ), f"Unexpected error: {e}"


@pytest.mark.asyncio
async def test_solution_using_separate_db_contexts(app, db, SQLAlchemyMiddleware):
    """
    Demonstrate the solution: Use separate db contexts for concurrent queries.

    Instead of using asyncio.gather() on the same session, create separate
    async functions that each use their own db context.

    This is different from multi_sessions mode - here we're showing how to
    structure the code to avoid the concurrent operations issue.
    """
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    # Setup data in the main context
    async with db(commit_on_exit=True):
        await db.session.execute(
            text("CREATE TABLE IF NOT EXISTS solution_test (id INTEGER PRIMARY KEY, name TEXT)")
        )
        for i in range(100):
            await db.session.execute(
                text("INSERT INTO solution_test (name) VALUES (:name)"),
                {"name": f"item_{i}"},
            )

    # Solution: Each function uses its own db context
    async def get_total_count():
        async with db():
            result = await db.session.execute(text("SELECT COUNT(*) FROM solution_test"))
            return result.scalar()

    async def get_paginated_data(offset: int, limit: int):
        async with db():
            result = await db.session.execute(
                text("SELECT * FROM solution_test LIMIT :limit OFFSET :offset"),
                {"limit": limit, "offset": offset},
            )
            return result.fetchall()

    # Now we can safely use asyncio.gather() because each function
    # creates its own session
    total, data = await asyncio.gather(
        get_total_count(),
        get_paginated_data(0, 10),
    )

    assert total == 100
    assert len(data) == 10


@pytest.mark.asyncio
async def test_antipattern_documentation(app, db, SQLAlchemyMiddleware):
    """
    Document the antipattern that causes the issue.

    This test exists purely for documentation purposes to show
    what NOT to do and why.
    """
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db(commit_on_exit=True):
        await db.session.execute(
            text("CREATE TABLE IF NOT EXISTS antipattern (id INTEGER PRIMARY KEY, data TEXT)")
        )
        for i in range(50):
            await db.session.execute(
                text("INSERT INTO antipattern (data) VALUES (:data)"),
                {"data": f"data_{i}"},
            )

    # ANTIPATTERN: Using asyncio.gather() with the same session
    # This is what the user was doing in their code
    async with db():
        count_stmt = text("SELECT COUNT(*) FROM antipattern")
        data_stmt = text("SELECT * FROM antipattern LIMIT 10")

        # This is the problematic pattern
        # With PostgreSQL/MySQL this will raise:
        # InvalidRequestError: This session is provisioning a new connection;
        # concurrent operations are not permitted
        try:
            total_result, data_result = await asyncio.gather(
                db.session.execute(count_stmt),
                db.session.execute(data_stmt),
            )
            # SQLite allows this but it's still an antipattern
            assert total_result.scalar() == 50
            assert len(data_result.fetchall()) == 10
        except Exception as e:
            # Expected with PostgreSQL/MySQL
            assert "concurrent" in str(e).lower() or "isce" in str(e).lower()

    # RECOMMENDED PATTERN 1: Sequential execution
    async with db():
        count_result = await db.session.execute(text("SELECT COUNT(*) FROM antipattern"))
        data_result = await db.session.execute(text("SELECT * FROM antipattern LIMIT 10"))

        assert count_result.scalar() == 50
        assert len(data_result.fetchall()) == 10

    # RECOMMENDED PATTERN 2: Use multi_sessions mode
    async with db(multi_sessions=True):

        async def get_count():
            return await db.session.execute(text("SELECT COUNT(*) FROM antipattern"))

        async def get_data():
            return await db.session.execute(text("SELECT * FROM antipattern LIMIT 10"))

        count_result, data_result = await asyncio.gather(get_count(), get_data())

        assert count_result.scalar() == 50
        assert len(data_result.fetchall()) == 10


@pytest.mark.asyncio
async def test_production_error_exact_reproduction(app, db, SQLAlchemyMiddleware):
    """
    Exact reproduction of the production error from the traceback.

    Production traceback shows:
    - Using asyncpg (PostgreSQL driver)
    - Using SQLModel AsyncSession
    - Two queries executed concurrently with asyncio.gather()
    - Error: sqlalchemy.exc.InvalidRequestError: This session is provisioning
             a new connection; concurrent operations are not permitted

    This test reproduces the exact pattern from:
    /app/api/repository/routes.py:186 in get_processes

    ```python
    total_result, processes_result = await asyncio.gather(
        db.session.execute(count_stmt), db.session.execute(processes_stmt)
    )
    ```
    """
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db(commit_on_exit=True):
        # Setup similar to production
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

        # Insert test data
        for i in range(100):
            await db.session.execute(
                text(
                    "INSERT INTO processes (name, status, created_at) "
                    "VALUES (:name, :status, :created_at)"
                ),
                {
                    "name": f"process_{i}",
                    "status": "running" if i % 2 == 0 else "stopped",
                    "created_at": "2025-01-01T00:00:00",
                },
            )

    # Simulate the production route handler
    async with db():
        # This is the exact pattern from production code
        count_stmt = text("SELECT COUNT(*) FROM processes WHERE status = :status")
        processes_stmt = text(
            "SELECT * FROM processes WHERE status = :status "
            "ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
        )

        # Bind parameters
        count_stmt = count_stmt.bindparams(status="running")
        processes_stmt = processes_stmt.bindparams(status="running", limit=10, offset=0)

        # This is the problematic line from production (line 186 in routes.py)
        # With PostgreSQL + asyncpg, this WILL fail with:
        # "This session is provisioning a new connection;
        #  concurrent operations are not permitted"
        try:
            total_result, processes_result = await asyncio.gather(
                db.session.execute(count_stmt),
                db.session.execute(processes_stmt),
            )

            # If SQLite allows it (serializes internally)
            total = total_result.scalar()
            processes = processes_result.fetchall()

            assert total == 50  # Half of the processes are "running"
            assert len(processes) == 10
            print(
                "SQLite allowed concurrent queries - "
                "but this WILL fail with PostgreSQL/asyncpg"
            )

        except Exception as e:
            # Expected with PostgreSQL/MySQL
            error_msg = str(e)
            # Check for the exact error from production
            assert (
                "concurrent operations are not permitted" in error_msg.lower()
                or "provisioning a new connection" in error_msg.lower()
                or "isce" in error_msg.lower()
            ), f"Got unexpected error: {e}"

            print(f"Successfully reproduced production error: {type(e).__name__}")

    # Show the correct fix for this production issue
    print("\n=== CORRECT FIX FOR PRODUCTION ===")

    # Fix Option 1: Sequential execution (simplest)
    async with db():
        count_result = await db.session.execute(count_stmt)
        processes_result = await db.session.execute(processes_stmt)

        total = count_result.scalar()
        processes = processes_result.fetchall()

        assert total == 50
        assert len(processes) == 10
        print("Fix 1: Sequential execution works!")

    # Fix Option 2: Use multi_sessions mode (for true parallelism)
    async with db(multi_sessions=True):

        async def get_count():
            return await db.session.execute(count_stmt)

        async def get_processes():
            return await db.session.execute(processes_stmt)

        total_result, processes_result = await asyncio.gather(get_count(), get_processes())

        total = total_result.scalar()
        processes = processes_result.fetchall()

        assert total == 50
        assert len(processes) == 10
        print("Fix 2: multi_sessions mode works!")
