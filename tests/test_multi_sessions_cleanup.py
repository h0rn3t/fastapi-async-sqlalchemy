import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

DB_URL = "sqlite+aiosqlite://"


@pytest.mark.parametrize("commit_on_exit", [True, False])
@pytest.mark.asyncio
async def test_multi_sessions_all_sessions_closed(app, SQLAlchemyMiddleware, db, commit_on_exit):
    """Ensure that every session created in multi_sessions mode is closed when context exits.

    We monkeypatch the AsyncSession (and SQLModel's AsyncSession if present) to track:
    - How many session instances are created
    - Which of them had .close() invoked
    Then we assert all created sessions were closed after the context manager exits.
    """
    app.add_middleware(SQLAlchemyMiddleware, db_url=DB_URL, commit_on_exit=commit_on_exit)

    created_sessions = []
    closed_sessions = set()

    # Collect target session classes (SQLAlchemy + optional SQLModel variant)
    target_classes = []
    target_classes.append(AsyncSession)
    try:
        from sqlmodel.ext.asyncio.session import (
            AsyncSession as SQLModelAsyncSession,  # type: ignore
        )

        target_classes.append(
            SQLModelAsyncSession
        )  # pragma: no cover - depends on optional dependency
    except Exception:  # pragma: no cover - sqlmodel may not be installed
        pass

    # Preserve originals for restore
    originals = {}
    for cls in target_classes:
        originals[(cls, "__init__")] = cls.__init__
        originals[(cls, "close")] = cls.close

        def make_init(original):
            def _init(self, *args, **kwargs):  # noqa: D401
                created_sessions.append(self)
                return original(self, *args, **kwargs)

            return _init

        async def make_close(original, self):  # type: ignore
            closed_sessions.add(self)
            return await original(self)

        # Assign patched methods
        cls.__init__ = make_init(cls.__init__)  # type: ignore

        async def _close(self, __original=cls.close):  # type: ignore
            closed_sessions.add(self)
            return await __original(self)

        cls.close = _close  # type: ignore

    try:
        async with db(multi_sessions=True, commit_on_exit=commit_on_exit):

            async def worker():
                # Access session multiple times in same task to create distinct sessions
                s1 = db.session
                s2 = db.session
                # Execute trivial queries
                await s1.execute(text("SELECT 1"))
                await s2.execute(text("SELECT 1"))

            tasks = [asyncio.create_task(worker()) for _ in range(5)]
            await asyncio.gather(*tasks)

        # After context exit all tracked sessions should be closed
        assert created_sessions, "No sessions were created in multi_sessions test."
        assert all(s in closed_sessions for s in created_sessions), (
            "Not all sessions were closed. "
            f"Created: {len(created_sessions)}, Closed: {len(closed_sessions)}"
        )
    finally:
        # Restore original methods to avoid side effects on other tests
        for cls in target_classes:
            cls.__init__ = originals[(cls, "__init__")]  # type: ignore
            cls.close = originals[(cls, "close")]  # type: ignore
