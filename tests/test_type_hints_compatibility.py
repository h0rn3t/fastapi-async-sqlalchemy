"""
Tests for type hints backwards compatibility (Issue #18)
https://github.com/h0rn3t/fastapi-async-sqlalchemy/issues/18
"""

import pytest

from fastapi_async_sqlalchemy import (
    DBSessionMeta,
    DBSessionType,
    db,
)


def test_dbsessionmeta_is_exported():
    """Test that DBSessionMeta is available for import"""
    assert DBSessionMeta is not None
    assert isinstance(DBSessionMeta, type)


def test_dbsessiontype_is_exported():
    """Test that DBSessionType is available for import (alternative name)"""
    assert DBSessionType is not None
    assert isinstance(DBSessionType, type)


def test_dbsessionmeta_equals_dbsessiontype():
    """Test that both names refer to the same type"""
    assert DBSessionMeta is DBSessionType


def test_type_of_db_is_dbsessionmeta():
    """Test that db instance has DBSessionMeta as its type"""
    assert type(db) is DBSessionMeta
    assert isinstance(db, DBSessionMeta)


def test_type_hints_work():
    """Test that type hints work correctly"""

    # This should work without type errors
    def get_db() -> DBSessionMeta:
        return db

    # Verify it returns the correct type
    result = get_db()
    assert result is db
    assert type(result) is DBSessionMeta


def test_alternative_type_hint_name():
    """Test that DBSessionType works as type hint"""

    def get_db_session() -> DBSessionType:
        return db

    result = get_db_session()
    assert result is db
    assert type(result) is DBSessionType


def test_backwards_compatibility_with_old_code():
    """
    Test backwards compatibility with code from v0.5
    This simulates the use case from issue #18
    """

    class MyClass:
        def __init__(self):
            self.db = db

        # This pattern from v0.5 should now work again
        def get_db(self) -> DBSessionMeta:
            return self.db

    instance = MyClass()
    result = instance.get_db()

    assert result is db
    assert type(result) is DBSessionMeta


def test_type_checking_with_callable():
    """Test that DBSessionMeta works with callable type checking"""
    from typing import Callable

    def factory() -> Callable[[], DBSessionMeta]:  # type: ignore[valid-type]
        def get_session() -> DBSessionMeta:  # type: ignore[valid-type]
            return db

        return get_session

    get_session = factory()
    result = get_session()

    assert result is db
    assert type(result) is DBSessionMeta


@pytest.mark.asyncio
async def test_type_hints_work_in_async_context():
    """Test type hints work correctly in async context"""

    async def get_db_async() -> DBSessionMeta:
        return db

    result = await get_db_async()
    assert result is db


def test_custom_middleware_with_type_hints():
    """Test that type hints work with custom middleware instances"""
    from fastapi_async_sqlalchemy.middleware import create_middleware_and_session_proxy

    CustomMiddleware, custom_db = create_middleware_and_session_proxy()

    # Type should match DBSessionMeta
    assert type(custom_db).__name__ == "DBSessionMeta"

    # Should be compatible with DBSessionMeta type hint
    def get_custom_db() -> DBSessionMeta:
        return custom_db

    result = get_custom_db()
    assert result is custom_db


def test_dbsessionmeta_has_correct_attributes():
    """Test that DBSessionMeta has the expected metaclass attributes"""
    assert hasattr(DBSessionMeta, "__mro__")
    assert hasattr(DBSessionMeta, "__name__")

    # Check that db class has session property (don't access it, just check it exists)
    assert "session" in dir(type(db))


def test_type_annotation_in_function_signature():
    """Test various type annotation patterns"""

    # Pattern 1: Return type
    def func1() -> DBSessionMeta:
        return db

    # Pattern 2: Parameter type
    def func2(session_proxy: DBSessionMeta) -> bool:
        return session_proxy is db

    # Pattern 3: Optional type
    from typing import Optional

    def func3() -> Optional[DBSessionMeta]:
        return db

    # Verify all patterns work
    assert func1() is db
    assert func2(db) is True
    assert func3() is db


def test_integration_with_fastapi_dependency():
    """Test using DBSessionMeta in FastAPI dependency injection"""

    def get_db_dependency() -> DBSessionMeta:
        """Dependency that returns db with proper type hint"""
        return db

    # Verify the dependency function works with type hints
    dependency_result = get_db_dependency()
    assert dependency_result is db
    assert type(dependency_result) is DBSessionMeta

    # Verify type hint is correct
    import inspect

    sig = inspect.signature(get_db_dependency)
    assert sig.return_annotation is DBSessionMeta


def test_module_and_qualname():
    """Test that DBSessionMeta has correct module and qualname"""
    assert DBSessionMeta.__module__ == "fastapi_async_sqlalchemy.middleware"
    assert DBSessionMeta.__name__ == "DBSessionMeta"


def test_type_identity():
    """Test that type(db) consistently returns the same type object"""
    type1 = type(db)
    type2 = type(db)

    assert type1 is type2
    assert type1 is DBSessionMeta
    assert type2 is DBSessionMeta


def test_isinstance_check():
    """Test isinstance checks work correctly"""
    assert isinstance(db, DBSessionMeta)
    assert isinstance(db, type(db))


def test_multiple_imports():
    """Test that importing DBSessionMeta multiple times works"""
    from fastapi_async_sqlalchemy import (
        DBSessionMeta as Meta1,
        DBSessionMeta as Meta2,
        db as db1,
        db as db2,
    )

    assert Meta1 is Meta2
    assert db1 is db2
    assert type(db1) is Meta1
    assert type(db2) is Meta2
