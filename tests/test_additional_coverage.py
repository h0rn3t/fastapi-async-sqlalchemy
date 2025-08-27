"""
Additional tests to reach target coverage of 97.22%
"""
import pytest
from fastapi import FastAPI


def test_commit_on_exit_parameter():
    """Test commit_on_exit parameter in middleware initialization"""
    from sqlalchemy.ext.asyncio import create_async_engine
    from fastapi_async_sqlalchemy.middleware import create_middleware_and_session_proxy
    
    SQLAlchemyMiddleware, db = create_middleware_and_session_proxy()
    app = FastAPI()
    
    # Test commit_on_exit=True
    custom_engine = create_async_engine("sqlite+aiosqlite://")
    middleware = SQLAlchemyMiddleware(app, custom_engine=custom_engine, commit_on_exit=True)
    assert middleware.commit_on_exit is True
    
    # Test commit_on_exit=False (default)
    middleware2 = SQLAlchemyMiddleware(app, custom_engine=custom_engine, commit_on_exit=False)
    assert middleware2.commit_on_exit is False


def test_exception_classes_simple():
    """Test exception classes are properly defined"""
    from fastapi_async_sqlalchemy.exceptions import MissingSessionError, SessionNotInitialisedError
    
    # Test exception instantiation without parameters
    missing_error = MissingSessionError()
    assert isinstance(missing_error, Exception)
    
    init_error = SessionNotInitialisedError()
    assert isinstance(init_error, Exception)


def test_middleware_properties():
    """Test middleware properties and methods"""
    from fastapi_async_sqlalchemy.middleware import create_middleware_and_session_proxy
    from sqlalchemy.ext.asyncio import create_async_engine
    from fastapi import FastAPI
    
    SQLAlchemyMiddleware, db = create_middleware_and_session_proxy()
    app = FastAPI()
    
    # Test middleware properties
    custom_engine = create_async_engine("sqlite+aiosqlite://")
    middleware = SQLAlchemyMiddleware(
        app, 
        custom_engine=custom_engine, 
        commit_on_exit=True
    )
    
    assert hasattr(middleware, 'commit_on_exit')
    assert middleware.commit_on_exit is True


def test_basic_imports():
    """Test basic imports and module structure"""
    # Test main module imports
    from fastapi_async_sqlalchemy import SQLAlchemyMiddleware, db
    assert SQLAlchemyMiddleware is not None
    assert db is not None
    
    # Test exception imports
    from fastapi_async_sqlalchemy.exceptions import MissingSessionError, SessionNotInitialisedError
    assert MissingSessionError is not None 
    assert SessionNotInitialisedError is not None
    
    # Test middleware module imports
    from fastapi_async_sqlalchemy.middleware import create_middleware_and_session_proxy, DefaultAsyncSession
    assert create_middleware_and_session_proxy is not None
    assert DefaultAsyncSession is not None


def test_middleware_factory_different_instances():
    """Test creating multiple middleware/db instances"""
    from fastapi_async_sqlalchemy.middleware import create_middleware_and_session_proxy
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import create_async_engine
    
    # Create first instance
    SQLAlchemyMiddleware1, db1 = create_middleware_and_session_proxy()
    
    # Create second instance
    SQLAlchemyMiddleware2, db2 = create_middleware_and_session_proxy()
    
    # They should be different instances
    assert SQLAlchemyMiddleware1 is not SQLAlchemyMiddleware2
    assert db1 is not db2
    
    # Test both instances work
    app = FastAPI()
    engine = create_async_engine("sqlite+aiosqlite://")
    
    middleware1 = SQLAlchemyMiddleware1(app, custom_engine=engine)
    middleware2 = SQLAlchemyMiddleware2(app, custom_engine=engine)
    
    assert middleware1 is not middleware2