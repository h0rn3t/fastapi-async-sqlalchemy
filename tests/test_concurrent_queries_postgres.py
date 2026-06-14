"""Real PostgreSQL/asyncpg reproduction of the `isce` concurrent-operations error.

The production traceback originates here::

    sqlalchemy.exc.InvalidRequestError: This session is provisioning a new
    connection; concurrent operations are not permitted
    (https://sqlalche.me/e/20/isce)

The error is triggered by ``await asyncio.gather(session.execute(...),
session.execute(...))`` against a single AsyncSession backed by asyncpg.
SQLite/aiosqlite serialises internally and does NOT reliably reproduce it
(see ``test_concurrent_queries.py``), so this module targets a real Postgres
instance and is skipped unless ``POSTGRES_TEST_URL`` is set.

Run locally::

    POSTGRES_TEST_URL="postgresql+asyncpg://user:pass@localhost:5432/test" \\
        pytest tests/test_concurrent_queries_postgres.py -v
"""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text

POSTGRES_URL = os.getenv("POSTGRES_TEST_URL")

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="POSTGRES_TEST_URL not set; this test requires a real PostgreSQL/asyncpg instance",
)


@pytest.fixture
def table_name():
    return f"isce_repro_{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def setup_table(app, db, SQLAlchemyMiddleware, table_name):
    SQLAlchemyMiddleware(app, db_url=POSTGRES_URL)
    async with db(commit_on_exit=True):
        await db.session.execute(
            text(f"CREATE TABLE {table_name} (id SERIAL PRIMARY KEY, name TEXT NOT NULL)")
        )
        await db.session.execute(
            text(
                f"INSERT INTO {table_name} (name) "
                "SELECT 'row_' || g FROM generate_series(1, 50) g"
            )
        )
    try:
        yield
    finally:
        async with db(commit_on_exit=True):
            await db.session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))


@pytest.mark.asyncio
async def test_gather_on_same_session_raises_isce(db, table_name, setup_table):
    """asyncio.gather() on a single AsyncSession must raise isce on asyncpg."""
    count_stmt = text(f"SELECT COUNT(*) FROM {table_name}")
    rows_stmt = text(f"SELECT id, name FROM {table_name} LIMIT 10")

    with pytest.raises(Exception) as exc_info:
        async with db():
            await asyncio.gather(
                db.session.execute(count_stmt),
                db.session.execute(rows_stmt),
            )

    error_msg = str(exc_info.value).lower()
    assert (
        "concurrent operations are not permitted" in error_msg
        or "provisioning a new connection" in error_msg
        or "isce" in error_msg
    ), f"Expected isce error, got: {exc_info.value!r}"


@pytest.mark.asyncio
async def test_db_gather_multi_sessions_avoids_isce(db, table_name, setup_table):
    """db.gather() in multi_sessions mode gives each task its own session — no isce."""
    count_stmt = text(f"SELECT COUNT(*) FROM {table_name}")
    rows_stmt = text(f"SELECT id, name FROM {table_name} LIMIT 10")

    async def get_count():
        result = await db.session.execute(count_stmt)
        return result.scalar()

    async def get_rows():
        result = await db.session.execute(rows_stmt)
        return result.fetchall()

    async with db(multi_sessions=True, max_concurrent=2):
        count, rows = await db.gather(get_count(), get_rows())

    assert count == 50
    assert len(rows) == 10


@pytest.mark.asyncio
async def test_sequential_execute_on_same_session_works(db, table_name, setup_table):
    """Sequential awaits on the same session never trigger isce."""
    async with db():
        count_result = await db.session.execute(text(f"SELECT COUNT(*) FROM {table_name}"))
        rows_result = await db.session.execute(text(f"SELECT id FROM {table_name} LIMIT 5"))

        assert count_result.scalar() == 50
        assert len(rows_result.fetchall()) == 5
