"""Regression tests for two specific bugs:

1. Streaming response generators that need database access must own an
   explicit ``async with db()`` body-lifetime session.

2. ``DBSession.__aexit__`` cancelled tasks already in ``state.task_sessions``
   but ignored tasks parked on the semaphore inside ``db.connection()``.
   A waiter could acquire the slot freed by a cancelled task, create a
   session post-closing, and run queries against state being torn down.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import text

db_url = "sqlite+aiosqlite://"


@pytest.mark.asyncio
async def test_streaming_response_keeps_session_open(app, db, SQLAlchemyMiddleware):
    """An explicit streaming DB session must outlive a StreamingResponse body."""
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    @app.get("/stream")
    async def stream():
        async def body():
            async with db():
                for i in range(3):
                    result = await db.session.execute(text(f"SELECT {i}"))
                    yield f"{result.scalar()}\n".encode()

        return StreamingResponse(body(), media_type="text/plain")

    with TestClient(app) as client:
        resp = client.get("/stream")
        assert resp.status_code == 200
        assert resp.text == "0\n1\n2\n"


@pytest.mark.asyncio
async def test_semaphore_waiters_cancelled_on_shutdown(app, db, SQLAlchemyMiddleware):
    """Tasks parked on db.connection()'s semaphore must be cancelled when
    the owning multi-session context exits, and must not create a session
    after closing has begun."""
    SQLAlchemyMiddleware(app, db_url=db_url)

    holder_started = asyncio.Event()
    holder_release = asyncio.Event()
    waiter_started = asyncio.Event()
    waiter_ran_query = False

    async def holder():
        async with db.connection() as session:
            holder_started.set()
            await session.execute(text("SELECT 1"))
            await holder_release.wait()

    async def waiter():
        nonlocal waiter_ran_query
        waiter_started.set()
        async with db.connection() as session:
            # Should never get here once shutdown begins.
            await session.execute(text("SELECT 2"))
            waiter_ran_query = True

    async with db(multi_sessions=True, max_concurrent=1):
        holder_task = asyncio.create_task(holder())
        await holder_started.wait()

        waiter_task = asyncio.create_task(waiter())
        # Give the waiter a chance to enter __aenter__ and park on acquire().
        await waiter_started.wait()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Let the holder finish so its slot is released.
        holder_release.set()
        await holder_task

    # __aexit__ has now run; the waiter must not have created a session.
    assert waiter_ran_query is False
    assert waiter_task.done()
    # On a clean context exit, __aexit__ cancels tasks parked on the
    # semaphore deterministically — the waiter must be reported as cancelled.
    assert waiter_task.cancelled()
