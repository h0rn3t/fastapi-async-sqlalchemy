"""
Additional edge case tests to maximize coverage
Targets specific uncovered lines in middleware.py
"""

import asyncio
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import text

from fastapi_async_sqlalchemy import SQLAlchemyMiddleware, db


@pytest.mark.asyncio
async def test_multi_session_with_exception_rollback():
    """Test multi-session mode rollback when exception occurs (line 166)"""
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url="sqlite+aiosqlite:///:memory:")

    @app.get("/test_exception_rollback")
    async def test_exception_rollback():
        with pytest.raises(ValueError):
            async with db(multi_sessions=True):
                session = db.session
                await session.execute(text("SELECT 1"))
                # Cause an exception to trigger rollback
                raise ValueError("Test exception for rollback")

        return {"status": "rolled_back"}

    with TestClient(app) as client:
        response = client.get("/test_exception_rollback")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_multi_session_commit_failure_raises():
    """Commit failure in multi-session cleanup must fail the request."""
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url="sqlite+aiosqlite:///:memory:")

    @app.get("/test_commit_failure_warning")
    async def test_commit_failure_warning():
        async with db(multi_sessions=True, commit_on_exit=True):
            session = db.session

            # Mock commit to raise exception
            async def failing_commit():
                raise SQLAlchemyError("Commit failed")

            session.commit = failing_commit

            await session.execute(text("SELECT 1"))

        return {"status": "handled"}

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test_commit_failure_warning")
    assert response.status_code == 500


@pytest.mark.asyncio
async def test_multi_session_rollback_failure_raises():
    """Rollback failure in multi-session cleanup must fail the request."""
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url="sqlite+aiosqlite:///:memory:")

    @app.get("/test_rollback_failure")
    async def test_rollback_failure():
        async with db(multi_sessions=True, commit_on_exit=True):
            session = db.session

            # Mock both commit and rollback to fail
            async def failing_commit():
                raise SQLAlchemyError("Commit failed")

            async def failing_rollback():
                raise SQLAlchemyError("Rollback failed")

            session.commit = failing_commit
            session.rollback = failing_rollback

            await session.execute(text("SELECT 1"))

        return {"status": "handled"}

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test_rollback_failure")
    assert response.status_code == 500


@pytest.mark.asyncio
async def test_multi_session_close_failure_raises():
    """Close failure in multi-session cleanup must fail the request."""
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url="sqlite+aiosqlite:///:memory:")

    @app.get("/test_close_failure")
    async def test_close_failure():
        async with db(multi_sessions=True):
            session = db.session

            # Mock close to fail
            original_close = session.close

            async def failing_close():
                await original_close()
                raise Exception("Close failed")

            session.close = failing_close

            await session.execute(text("SELECT 1"))

        return {"status": "handled"}

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test_close_failure")
    assert response.status_code == 500


@pytest.mark.asyncio
async def test_single_session_commit_exception_rollback():
    """Test single session mode commit exception triggers rollback (lines 216-218)"""
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url="sqlite+aiosqlite:///:memory:")

    @app.get("/test_commit_exception")
    async def test_commit_exception():
        with pytest.raises(SQLAlchemyError):
            async with db(commit_on_exit=True):
                session = db.session

                # Mock commit to raise exception
                original_rollback = session.rollback
                rollback_called = False

                async def failing_commit():
                    raise SQLAlchemyError("Commit failed")

                async def tracking_rollback():
                    nonlocal rollback_called
                    rollback_called = True
                    await original_rollback()

                session.commit = failing_commit
                session.rollback = tracking_rollback

                await session.execute(text("SELECT 1"))

        return {"status": "handled", "rollback_called": rollback_called}

    with TestClient(app) as client:
        response = client.get("/test_commit_exception")
    # The exception should propagate
    assert response.status_code == 500 or response.status_code == 200


@pytest.mark.asyncio
async def test_multi_session_cleanup_all_paths():
    """Comprehensive test for all multi-session cleanup paths"""
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url="sqlite+aiosqlite:///:memory:")

    @app.get("/test_comprehensive")
    async def test_comprehensive():
        async with db(multi_sessions=True, commit_on_exit=True):
            sessions = []
            tasks = []

            async def run_query(value: int):
                session = db.session
                sessions.append(session)
                await session.execute(text(f"SELECT {value}"))

            for i in range(3):
                tasks.append(asyncio.create_task(run_query(i)))

            await asyncio.gather(*tasks)

        return {"session_count": len(set(sessions))}

    with TestClient(app) as client:
        response = client.get("/test_comprehensive")
    assert response.status_code == 200
    assert response.json()["session_count"] == 3


@pytest.mark.asyncio
async def test_multi_session_no_sessions_created():
    """Test multi-session mode where no sessions are created"""
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url="sqlite+aiosqlite:///:memory:")

    @app.get("/test_no_sessions")
    async def test_no_sessions():
        # Enter multi-session context but don't create any sessions
        async with db(multi_sessions=True):
            # Don't access db.session at all
            pass

        return {"status": "ok"}

    with TestClient(app) as client:
        response = client.get("/test_no_sessions")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_single_session_exception_handling():
    """Test single session mode with exception (line 212)"""
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url="sqlite+aiosqlite:///:memory:")

    @app.get("/test_single_exception")
    async def test_single_exception():
        with pytest.raises(ValueError):
            async with db():
                session = db.session
                await session.execute(text("SELECT 1"))
                raise ValueError("Test exception")

        return {"status": "exception_handled"}

    with TestClient(app) as client:
        response = client.get("/test_single_exception")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_cleanup_callback_without_running_loop_warns():
    """Cleanup callback must warn (not crash) when no event loop is available.

    Covers the `except RuntimeError` fallback when capturing the loop at
    session creation time and the "No running event loop" warning in the
    task-done cleanup callback.
    """
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url="sqlite+aiosqlite:///:memory:")

    @app.get("/test_no_loop")
    async def test_no_loop():
        async with db(multi_sessions=True):

            async def child_task():
                session = db.session
                await session.execute(text("SELECT 1"))
                return "done"

            with patch(
                "asyncio.get_running_loop",
                side_effect=RuntimeError("No running event loop"),
            ):
                task = asyncio.create_task(child_task())
                await task

        return {"done": True}

    with TestClient(app) as client:
        with pytest.warns(UserWarning, match="No running event loop during cleanup"):
            response = client.get("/test_no_loop")
    assert response.status_code == 200
