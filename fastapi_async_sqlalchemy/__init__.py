from fastapi_async_sqlalchemy.middleware import (
    SQLAlchemyMiddleware,
    create_middleware_and_session_proxy,
    db,
)

# Export DBSessionMeta type for type hints (Issue #18)
# Note: DBSessionMeta is the metaclass of db, created dynamically.
# It can be used in runtime type checks (isinstance, type(db) is DBSessionMeta)
# but mypy may show warnings when used in type annotations due to its dynamic nature.
DBSessionMeta = type(db)
DBSessionType = DBSessionMeta  # Alternative name for backwards compatibility

__all__ = [
    "db",
    "SQLAlchemyMiddleware",
    "create_middleware_and_session_proxy",
    "DBSessionMeta",
    "DBSessionType",
]

__version__ = "0.7.2alpha"
