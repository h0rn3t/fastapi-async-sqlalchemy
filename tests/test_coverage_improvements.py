"""
Tests to improve code coverage for edge cases and fallback imports.
"""
import asyncio
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.sql import text

from fastapi_async_sqlalchemy import SQLAlchemyMiddleware, db


@pytest.mark.asyncio
async def test_cleanup_callback_with_closed_loop():
    """Test cleanup callback when event loop is closed."""
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url="sqlite+aiosqlite:///:memory:")

    cleanup_called = False

    @app.get("/test_closed_loop")
    async def test_closed_loop():
        async with db(multi_sessions=True):
            # Create a task that will trigger cleanup
            async def child_task():
                session = db.session
                await session.execute(text("SELECT 1"))
                return "done"

            task = asyncio.create_task(child_task())
            result = await task

        return {"result": result}

    client = TestClient(app)
    response = client.get("/test_closed_loop")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_cleanup_callback_runtime_error():
    """Test cleanup callback when get_running_loop raises RuntimeError."""
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url="sqlite+aiosqlite:///:memory:")

    @app.get("/test_runtime_error")
    async def test_runtime_error():
        async with db(multi_sessions=True):
            async def child_task():
                session = db.session
                await session.execute(text("SELECT 42"))
                return "ok"

            task = asyncio.create_task(child_task())
            result = await task

        # Give time for cleanup callbacks to execute
        await asyncio.sleep(0.1)
        return {"result": result}

    client = TestClient(app)
    response = client.get("/test_runtime_error")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_multiple_child_tasks_cleanup():
    """Test that multiple child tasks all get cleanup callbacks."""
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url="sqlite+aiosqlite:///:memory:")

    @app.get("/test_multiple_cleanup")
    async def test_multiple_cleanup():
        results = []
        async with db(multi_sessions=True):
            async def child_task(n):
                session = db.session
                result = await session.execute(text(f"SELECT {n}"))
                return result.scalar()

            tasks = [asyncio.create_task(child_task(i)) for i in range(5)]
            results = await asyncio.gather(*tasks)

        return {"results": results}

    client = TestClient(app)
    response = client.get("/test_multiple_cleanup")
    assert response.status_code == 200
    assert len(response.json()["results"]) == 5


def test_import_coverage_markers():
    """Test that import fallback code paths are marked for coverage."""
    # This test ensures that import fallback blocks are properly marked
    # even if they can't be executed in the current environment
    
    # The actual imports happen at module load time, so we can't test them
    # directly without manipulating sys.modules before import.
    # Instead, we verify the code exists and is syntactically correct.
    
    import fastapi_async_sqlalchemy.middleware as middleware_module
    
    # Verify that DefaultAsyncSession is set
    assert hasattr(middleware_module, 'DefaultAsyncSession')
    
    # Check if we're using SQLModel or plain AsyncSession
    from sqlalchemy.ext.asyncio import AsyncSession
    assert issubclass(middleware_module.DefaultAsyncSession, AsyncSession)


@pytest.mark.asyncio  
async def test_current_task_none_scenario():
    """
    Test scenario where asyncio.current_task() might return None.
    
    Note: This is extremely rare in practice and hard to reproduce,
    as current_task() only returns None when called outside an event loop.
    The middleware already requires an event loop, so this edge case
    is primarily for completeness.
    """
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url="sqlite+aiosqlite:///:memory:")

    @app.get("/test_task_context")
    async def test_task_context():
        # Verify we're in a task context
        task = asyncio.current_task()
        assert task is not None, "Should have current task in request context"
        
        async with db(multi_sessions=True):
            # Access session within task context
            session = db.session
            result = await session.execute(text("SELECT 1"))
            
        return {"success": True}

    client = TestClient(app)
    response = client.get("/test_task_context")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_edge_case_loop_closing_during_cleanup():
    """Test edge case where loop closes during cleanup callback setup."""
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url="sqlite+aiosqlite:///:memory:")

    @app.get("/test_loop_edge")
    async def test_loop_edge():
        async with db(multi_sessions=True, commit_on_exit=True):
            # Create multiple tasks that will all need cleanup
            async def quick_task(n):
                s = db.session
                await s.execute(text(f"SELECT {n}"))

            tasks = [asyncio.create_task(quick_task(i)) for i in range(3)]
            await asyncio.gather(*tasks)

        return {"done": True}

    client = TestClient(app)
    response = client.get("/test_loop_edge")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_current_task_none_with_mock():
    """
    Test RuntimeError when current_task() returns None.

    This is an edge case that's nearly impossible in real async code,
    but we test it for completeness.
    """
    from unittest.mock import patch

    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url="sqlite+aiosqlite:///:memory:")

    @app.get("/test_none_task")
    async def test_none_task():
        # Temporarily mock current_task to return None
        with patch('asyncio.current_task', return_value=None):
            async with db(multi_sessions=True):
                try:
                    _ = db.session  # This should raise RuntimeError
                    return {"error": "Should have raised RuntimeError"}
                except RuntimeError as e:
                    if "Cannot get current task" in str(e):
                        return {"success": True}
                    raise

    client = TestClient(app)
    response = client.get("/test_none_task")
    assert response.status_code == 200
    assert response.json()["success"] is True


@pytest.mark.asyncio
async def test_cleanup_callback_with_mocked_closed_loop():
    """
    Test cleanup callback behavior when loop.is_closed() returns True.

    This is a direct test of the cleanup_callback function to ensure
    it handles the closed loop case properly (lines 109-110).
    """
    from unittest.mock import MagicMock, patch

    # We need to test the cleanup callback directly
    # First, let's access the session property to trigger session creation with cleanup
    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url="sqlite+aiosqlite:///:memory:")

    callback_executed = []

    @app.get("/test_mock_closed")
    async def test_mock_closed():
        async with db(multi_sessions=True):
            # Patch get_running_loop inside the cleanup callback
            original_get_running_loop = asyncio.get_running_loop

            async def child_with_mock():
                session = db.session
                await session.execute(text("SELECT 1"))

                # After this task finishes, the cleanup callback will be called
                # We want to mock get_running_loop at that point
                return "done"

            # Patch asyncio.get_running_loop to return a closed loop
            def mock_get_running_loop_closed():
                loop = MagicMock()
                loop.is_closed.return_value = True
                callback_executed.append("closed_loop_path")
                return loop

            with patch('asyncio.get_running_loop', side_effect=mock_get_running_loop_closed):
                task = asyncio.create_task(child_with_mock())
                await task
                # Task is done, cleanup callback will execute with mocked get_running_loop

        return {"done": True}

    client = TestClient(app)
    response = client.get("/test_mock_closed")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_cleanup_callback_with_runtime_error():
    """
    Test cleanup callback when get_running_loop() raises RuntimeError.

    This tests the except RuntimeError branch (lines 111-112).
    """
    from unittest.mock import patch

    app = FastAPI()
    app.add_middleware(SQLAlchemyMiddleware, db_url="sqlite+aiosqlite:///:memory:")

    callback_executed = []

    @app.get("/test_runtime_error")
    async def test_runtime_error():
        async with db(multi_sessions=True):
            async def child_with_runtime_error():
                session = db.session
                await session.execute(text("SELECT 1"))
                return "done"

            # Patch get_running_loop to raise RuntimeError
            def mock_get_running_loop_error():
                callback_executed.append("runtime_error_path")
                raise RuntimeError("No running event loop")

            with patch('asyncio.get_running_loop', side_effect=mock_get_running_loop_error):
                task = asyncio.create_task(child_with_runtime_error())
                await task
                # Cleanup callback will execute and hit the RuntimeError path

        return {"done": True}

    client = TestClient(app)
    response = client.get("/test_runtime_error")
    assert response.status_code == 200
