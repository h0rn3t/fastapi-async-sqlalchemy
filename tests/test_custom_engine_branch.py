"""
Targeted test to ensure custom_engine branch (line 61) is executed
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.sql import text

from fastapi_async_sqlalchemy.middleware import create_middleware_and_session_proxy


@pytest.mark.asyncio
async def test_custom_engine_branch_with_actual_usage():
    """
    Ensure custom_engine else branch (line 61) is covered
    by actually using the middleware with a custom engine
    """
    # Create fresh middleware and db instances
    SQLAlchemyMiddleware, db = create_middleware_and_session_proxy()

    # Create a custom async engine
    custom_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    app = FastAPI()

    # Add middleware with custom_engine
    # This should execute: else: engine = custom_engine (line 61)
    app.add_middleware(SQLAlchemyMiddleware, custom_engine=custom_engine, commit_on_exit=False)

    # Create endpoint to test the session works
    @app.get("/test")
    async def test_endpoint():
        async with db():
            result = await db.session.execute(text("SELECT 42 as value"))
            value = result.scalar()
            return {"value": value}

    # Test the endpoint
    client = TestClient(app)
    response = client.get("/test")

    assert response.status_code == 200
    assert response.json()["value"] == 42


def test_custom_engine_without_db_url():
    """
    Verify custom_engine can be used without providing db_url
    This ensures the else branch is used
    """
    SQLAlchemyMiddleware, db = create_middleware_and_session_proxy()

    app = FastAPI()

    # Create custom engine
    custom_engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    # Initialize middleware with ONLY custom_engine (no db_url)
    # This should take the else branch at line 61
    middleware = SQLAlchemyMiddleware(
        app, custom_engine=custom_engine, engine_args={}, session_args={}
    )

    assert middleware is not None
    assert middleware.commit_on_exit is False


def test_custom_engine_with_session_args():
    """
    Test custom_engine with various session_args
    """
    SQLAlchemyMiddleware, db = create_middleware_and_session_proxy()

    app = FastAPI()

    custom_engine = create_async_engine("sqlite+aiosqlite://")

    # Use custom engine with session args
    middleware = SQLAlchemyMiddleware(
        app, custom_engine=custom_engine, session_args={"autoflush": False}, commit_on_exit=True
    )

    assert middleware is not None
    assert middleware.commit_on_exit is True


def test_custom_engine_multiple_instances():
    """
    Test multiple middleware instances with different custom engines
    """
    SQLAlchemyMiddleware1, db1 = create_middleware_and_session_proxy()
    SQLAlchemyMiddleware2, db2 = create_middleware_and_session_proxy()

    app = FastAPI()

    # Create two different custom engines
    engine1 = create_async_engine("sqlite+aiosqlite:///:memory:")
    engine2 = create_async_engine("sqlite+aiosqlite://")

    # Create two middleware instances
    middleware1 = SQLAlchemyMiddleware1(app, custom_engine=engine1)
    middleware2 = SQLAlchemyMiddleware2(app, custom_engine=engine2)

    assert middleware1 is not None
    assert middleware2 is not None
