"""Regression tests for FastAPI `yield` dependencies that own a transaction.

FastAPI runs `yield` dependency teardown *after* the response is sent, which is
also when the middleware finalizes the request session to free its connection
before background tasks. Finalizing there closed the session out from under a
dependency holding `async with db.session.begin()`, rolling its work back just
before its own commit — a 200 response with no row written.
"""

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from fastapi_async_sqlalchemy import create_middleware_and_session_proxy


async def _make_app(tmp_path, dependency_factory, commit_on_exit=False):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'yield_dep.db'}")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE entries (value INTEGER)"))

    middleware_class, db = create_middleware_and_session_proxy()
    app = FastAPI()
    app.add_middleware(middleware_class, custom_engine=engine, commit_on_exit=commit_on_exit)
    return engine, db, app


async def _rows(engine):
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT value FROM entries ORDER BY value"))
        return result.scalars().all()


@pytest.mark.asyncio
async def test_yield_dependency_transaction_commits(tmp_path):
    """`async with db.session.begin(): yield` must still commit after the response."""
    engine, db, app = await _make_app(tmp_path, None)

    async def transaction():
        async with db.session.begin():
            yield

    @app.post("/entries", dependencies=[Depends(transaction)])
    async def create_entry():
        await db.session.execute(text("INSERT INTO entries VALUES (1)"))
        return {"ok": True}

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/entries")
            assert response.status_code == 200

        assert await _rows(engine) == [1], "the dependency's commit was rolled back"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_yield_dependency_transaction_rolls_back_on_error(tmp_path):
    """A failing route must still roll the dependency's transaction back."""
    engine, db, app = await _make_app(tmp_path, None)

    async def transaction():
        async with db.session.begin():
            yield

    @app.post("/entries", dependencies=[Depends(transaction)])
    async def create_entry():
        await db.session.execute(text("INSERT INTO entries VALUES (1)"))
        raise RuntimeError("route failed")

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with pytest.raises(RuntimeError, match="route failed"):
                await client.post("/entries")

        assert await _rows(engine) == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_yield_dependency_can_still_use_session_after_response(tmp_path):
    """Teardown of a transaction-owning dependency may write, not just commit."""
    engine, db, app = await _make_app(tmp_path, None)

    async def transaction():
        async with db.session.begin():
            yield
            await db.session.execute(text("INSERT INTO entries VALUES (2)"))

    @app.post("/entries", dependencies=[Depends(transaction)])
    async def create_entry():
        await db.session.execute(text("INSERT INTO entries VALUES (1)"))
        return {"ok": True}

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post("/entries")).status_code == 200

        assert await _rows(engine) == [1, 2]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_nested_transaction_dependency_commits(tmp_path):
    """A savepoint held across the response must survive too."""
    engine, db, app = await _make_app(tmp_path, None, commit_on_exit=True)

    async def savepoint():
        await db.session.execute(text("SELECT 1"))  # autobegin the outer transaction
        async with db.session.begin_nested():
            yield

    @app.post("/entries", dependencies=[Depends(savepoint)])
    async def create_entry():
        await db.session.execute(text("INSERT INTO entries VALUES (1)"))
        return {"ok": True}

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post("/entries")).status_code == 200

        assert await _rows(engine) == [1]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_plain_dependency_still_finalizes_early(tmp_path):
    """Without an explicit transaction the early finalization must stay in place.

    That is what keeps a background task from pinning the request connection,
    so the deferral must be narrow — it applies only to owned transactions.
    """
    engine, db, app = await _make_app(tmp_path, None, commit_on_exit=True)
    seen = {}

    async def plain():
        yield
        from fastapi_async_sqlalchemy.exceptions import MissingSessionError

        try:
            _ = db.session
            seen["closed"] = False
        except (MissingSessionError, RuntimeError):
            seen["closed"] = True

    @app.post("/entries", dependencies=[Depends(plain)])
    async def create_entry():
        await db.session.execute(text("INSERT INTO entries VALUES (1)"))
        return {"ok": True}

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post("/entries")).status_code == 200

        assert seen["closed"] is True, "autobegun sessions must still finalize early"
        assert await _rows(engine) == [1]
    finally:
        await engine.dispose()
