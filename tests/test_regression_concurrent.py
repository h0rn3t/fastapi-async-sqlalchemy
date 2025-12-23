"""Regression test for concurrent query behavior change.

This test documents a breaking change in fastapi-async-sqlalchemy behavior
introduced in commit 4e99374 (Dec 21, 2025).

BREAKING CHANGE:
================

Before commit 4e99374:
- In multi_sessions=True mode, each access to db.session created a NEW session
- This allowed direct use of asyncio.gather() with db.session.execute()
- Pattern like this worked:
  ```python
  async with db(multi_sessions=True):
      r1, r2 = await asyncio.gather(
          db.session.execute(stmt1),
          db.session.execute(stmt2)
      )
  ```

After commit 4e99374:
- Sessions are now cached per task_id
- Multiple accesses to db.session in the same task return the SAME session
- Direct asyncio.gather() with db.session.execute() now fails with:
  "This session is provisioning a new connection; concurrent operations are not permitted"

MIGRATION:
==========

Old code (worked before 4e99374, FAILS after):
```python
async with db(multi_sessions=True):
    total_result, processes_result = await asyncio.gather(
        db.session.execute(count_stmt),
        db.session.execute(processes_stmt)
    )
```

New code (required after 4e99374):
```python
async with db(multi_sessions=True):
    async def get_count():
        return await db.session.execute(count_stmt)

    async def get_processes():
        return await db.session.execute(processes_stmt)

    total_result, processes_result = await asyncio.gather(
        get_count(),
        get_processes()
    )
```

OR (simpler, but sequential):
```python
async with db():
    total_result = await db.session.execute(count_stmt)
    processes_result = await db.session.execute(processes_stmt)
```
"""

import asyncio

import pytest
from sqlalchemy import text

db_url = "sqlite+aiosqlite://"


@pytest.mark.asyncio
async def test_old_behavior_documentation(app, db, SQLAlchemyMiddleware):
    """
    Document the OLD behavior that worked before commit 4e99374.

    This test shows what USED TO WORK but now FAILS.
    This is a BREAKING CHANGE that affects production code.
    """
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db(commit_on_exit=True):
        await db.session.execute(
            text("CREATE TABLE IF NOT EXISTS old_behavior (id INTEGER PRIMARY KEY, value TEXT)")
        )
        for i in range(10):
            await db.session.execute(
                text("INSERT INTO old_behavior (value) VALUES (:value)"),
                {"value": f"value_{i}"},
            )

    # OLD BEHAVIOR (worked before 4e99374):
    # Each db.session access created a NEW session in multi_sessions mode
    # This pattern worked:
    async with db(multi_sessions=True):
        count_stmt = text("SELECT COUNT(*) FROM old_behavior")
        data_stmt = text("SELECT * FROM old_behavior LIMIT 5")

        # This USED TO WORK because each db.session was a different session
        # After 4e99374, this FAILS because both calls get the SAME cached session
        try:
            count_result, data_result = await asyncio.gather(
                db.session.execute(count_stmt),
                db.session.execute(data_stmt),
            )
            # If it works with SQLite (which serializes internally)
            assert count_result.scalar() == 10
            assert len(data_result.fetchall()) == 5
            print("WARNING: Old pattern still works with SQLite but WILL FAIL with PostgreSQL")
        except Exception as e:
            # Expected with PostgreSQL/MySQL after the change
            error_msg = str(e).lower()
            assert any(
                phrase in error_msg
                for phrase in [
                    "concurrent operations are not permitted",
                    "isce",
                    "provisioning a new connection",
                ]
            )
            print(f"BREAKING CHANGE DETECTED: Old pattern now fails with: {type(e).__name__}")


@pytest.mark.asyncio
async def test_new_required_pattern(app, db, SQLAlchemyMiddleware):
    """
    Show the NEW pattern required after commit 4e99374.

    After the change, you MUST wrap each query in a separate async function
    to get a separate task (and thus a separate session).
    """
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db(commit_on_exit=True):
        await db.session.execute(
            text("CREATE TABLE IF NOT EXISTS new_pattern (id INTEGER PRIMARY KEY, value TEXT)")
        )
        for i in range(10):
            await db.session.execute(
                text("INSERT INTO new_pattern (value) VALUES (:value)"),
                {"value": f"value_{i}"},
            )

    # NEW REQUIRED PATTERN (after 4e99374):
    # Wrap each query in a separate async function to create separate tasks
    async with db(multi_sessions=True):

        async def get_count():
            # This creates a new task, so gets its own session
            result = await db.session.execute(text("SELECT COUNT(*) FROM new_pattern"))
            return result.scalar()

        async def get_data():
            # This creates a new task, so gets its own session
            result = await db.session.execute(text("SELECT * FROM new_pattern LIMIT 5"))
            return result.fetchall()

        # Now this works because each function runs in its own task
        count, data = await asyncio.gather(get_count(), get_data())

        assert count == 10
        assert len(data) == 5


@pytest.mark.asyncio
async def test_session_caching_per_task(app, db, SQLAlchemyMiddleware):
    """
    Demonstrate that sessions are now cached per task_id.

    This is the core behavioral change in commit 4e99374.
    """
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db(commit_on_exit=True):
        await db.session.execute(
            text("CREATE TABLE IF NOT EXISTS caching_test (id INTEGER PRIMARY KEY, value TEXT)")
        )

    async with db(multi_sessions=True):
        # Multiple accesses to db.session in the SAME task return the SAME session
        session1 = db.session
        session2 = db.session
        session3 = db.session

        # After 4e99374, all three should be the SAME session object
        assert session1 is session2
        assert session2 is session3

        print("Confirmed: Sessions are cached per task (new behavior)")


@pytest.mark.asyncio
async def test_different_tasks_get_different_sessions(app, db, SQLAlchemyMiddleware):
    """
    Verify that DIFFERENT tasks still get DIFFERENT sessions.

    This behavior is preserved in commit 4e99374.
    """
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    sessions = []

    async with db(multi_sessions=True):

        async def get_session():
            sessions.append(id(db.session))
            return id(db.session)

        # Each async function call creates a separate task
        task1_session, task2_session, task3_session = await asyncio.gather(
            get_session(),
            get_session(),
            get_session(),
        )

        # Different tasks should get different sessions
        assert task1_session != task2_session
        assert task2_session != task3_session
        assert task1_session != task3_session

        print("Confirmed: Different tasks get different sessions (behavior preserved)")


@pytest.mark.asyncio
async def test_migration_guide_example(app, db, SQLAlchemyMiddleware):
    """
    Complete migration example from old code to new code.

    This shows the exact changes needed in production code.
    """
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db(commit_on_exit=True):
        await db.session.execute(
            text("""
                CREATE TABLE IF NOT EXISTS migration_example (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    status TEXT
                )
            """)
        )
        for i in range(100):
            await db.session.execute(
                text("INSERT INTO migration_example (name, status) VALUES (:name, :status)"),
                {"name": f"item_{i}", "status": "active" if i % 2 == 0 else "inactive"},
            )

    # ❌ OLD CODE (worked before 4e99374, fails after with PostgreSQL):
    # async with db(multi_sessions=True):
    #     count_stmt = text("SELECT COUNT(*) FROM migration_example WHERE status = 'active'")
    #     data_stmt = text("SELECT * FROM migration_example WHERE status = 'active' LIMIT 10")
    #
    #     total_result, data_result = await asyncio.gather(
    #         db.session.execute(count_stmt),
    #         db.session.execute(data_stmt)
    #     )

    # ✅ NEW CODE (required after 4e99374):
    async with db(multi_sessions=True):
        count_stmt = text("SELECT COUNT(*) FROM migration_example WHERE status = 'active'")
        data_stmt = text("SELECT * FROM migration_example WHERE status = 'active' LIMIT 10")

        async def get_count():
            result = await db.session.execute(count_stmt)
            return result.scalar()

        async def get_data():
            result = await db.session.execute(data_stmt)
            return result.fetchall()

        # Each function creates its own task, so gets its own session
        total, data = await asyncio.gather(get_count(), get_data())

        assert total == 50
        assert len(data) == 10

    print("Migration complete: Old code pattern updated to new pattern")
