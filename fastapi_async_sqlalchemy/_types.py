"""Type hints for the public ``db`` session proxy (Issue #18).

The runtime ``DBSessionMeta`` is the metaclass produced by
``create_middleware_and_session_proxy`` and lives inside that closure, so it is
not visible to static type checkers. This module exposes a structural
``Protocol`` with the same public API so users can annotate code as::

    def get_db() -> DBSessionMeta:
        return db

and get full IDE autocomplete / mypy support on ``db.session``,
``db.connection()`` and ``db.gather()``.

At runtime ``fastapi_async_sqlalchemy.DBSessionMeta`` is rebound to
``type(db)`` (the actual metaclass) so ``isinstance(db, DBSessionMeta)``
keeps working as before.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession


@runtime_checkable
class DBSessionMeta(Protocol):
    """Structural type for the ``db`` session proxy.

    Use as a return / parameter annotation when you want to pass ``db``
    around with proper type hints instead of falling back to ``Any``.
    """

    @property
    def session(self) -> AsyncSession:
        """Return the ``AsyncSession`` bound to the current async context."""
        ...

    def connection(self) -> AbstractAsyncContextManager[AsyncSession]:
        """Async context manager that yields a throttled session."""
        ...

    async def gather(
        self,
        *coros_or_futures: Any,
        return_exceptions: bool = ...,
    ) -> list[Any]:
        """Pool-aware drop-in replacement for ``asyncio.gather``."""
        ...

    def __call__(
        self,
        session_args: dict[str, Any] | None = ...,
        commit_on_exit: bool = ...,
        multi_sessions: bool = ...,
        max_concurrent: int | None = ...,
    ) -> AbstractAsyncContextManager[Any]:
        """Open an explicit session context: ``async with db(): ...``."""
        ...


DBSessionType = DBSessionMeta
