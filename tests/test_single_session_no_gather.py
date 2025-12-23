"""Tests to ensure single session mode works correctly without multi_sessions=True.

These tests verify that the original behavior is preserved:
- Sequential queries work with a single session
- The same session instance is reused within a context
- asyncio.gather() without multi_sessions=True raises an error (expected behavior)
"""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IllegalStateChangeError, InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

db_url = "sqlite+aiosqlite://"


@pytest.mark.asyncio
async def test_single_session_sequential_queries(app, db, SQLAlchemyMiddleware):
    """Sequential queries should work with single session."""
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db():
        result1 = await db.session.execute(text("SELECT 1"))
        result2 = await db.session.execute(text("SELECT 2"))
        assert result1.scalar() == 1
        assert result2.scalar() == 2


@pytest.mark.asyncio
async def test_single_session_same_instance(app, db, SQLAlchemyMiddleware):
    """Same session instance should be returned within context."""
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db():
        session1 = db.session
        session2 = db.session
        assert session1 is session2
        assert isinstance(session1, AsyncSession)


@pytest.mark.asyncio
async def test_single_session_gather_fails(app, db, SQLAlchemyMiddleware):
    """asyncio.gather() without multi_sessions=True should raise an error."""
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    with pytest.raises((InvalidRequestError, IllegalStateChangeError)):
        async with db():
            await asyncio.gather(
                db.session.execute(text("SELECT 1")),
                db.session.execute(text("SELECT 2")),
            )


@pytest.mark.asyncio
async def test_multi_sessions_gather_with_tasks(app, db, SQLAlchemyMiddleware):
    """asyncio.gather() with multi_sessions=True and create_task should work."""
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db(multi_sessions=True):

        async def query(n):
            return await db.session.execute(text(f"SELECT {n}"))

        tasks = [
            asyncio.create_task(query(1)),
            asyncio.create_task(query(2)),
        ]
        results = await asyncio.gather(*tasks)
        assert results[0].scalar() == 1
        assert results[1].scalar() == 2


@pytest.mark.asyncio
async def test_single_session_in_route(app, client, db, SQLAlchemyMiddleware):
    """Single session should work in route handler."""
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    @app.get("/test")
    async def test_route():
        result = await db.session.execute(text("SELECT 42"))
        return {"value": result.scalar()}

    response = client.get("/test")
    assert response.status_code == 200
    assert response.json() == {"value": 42}


@pytest.mark.asyncio
async def test_single_session_multiple_sequential_in_route(app, client, db, SQLAlchemyMiddleware):
    """Multiple sequential queries in route should work."""
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    @app.get("/test-sequential")
    async def test_route():
        r1 = await db.session.execute(text("SELECT 1"))
        r2 = await db.session.execute(text("SELECT 2"))
        r3 = await db.session.execute(text("SELECT 3"))
        return {"values": [r1.scalar(), r2.scalar(), r3.scalar()]}

    response = client.get("/test-sequential")
    assert response.status_code == 200
    assert response.json() == {"values": [1, 2, 3]}
