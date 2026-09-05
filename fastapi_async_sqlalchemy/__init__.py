from typing import TYPE_CHECKING

from fastapi_async_sqlalchemy.middleware import (
    SQLAlchemyMiddleware,
    create_middleware_and_session_proxy,
    db,
)

# DBSessionMeta exposure (Issue #18).
#
# At type-check time, expose a structural Protocol so users get full mypy /
# IDE autocomplete on ``db.session``, ``db.connection()`` and ``db.gather()``
# when they annotate code with ``DBSessionMeta``.
#
# At runtime, expose the actual metaclass of ``db`` (a closure-local class
# created by ``create_middleware_and_session_proxy``) so ``isinstance(db,
# DBSessionMeta)`` and ``type(db) is DBSessionMeta`` keep working as in v0.5.
if TYPE_CHECKING:
    from fastapi_async_sqlalchemy._types import DBSessionMeta
else:
    DBSessionMeta = type(db)

__all__ = [
    "db",
    "SQLAlchemyMiddleware",
    "create_middleware_and_session_proxy",
    "DBSessionMeta",
]

__version__ = "0.8.0b1"
