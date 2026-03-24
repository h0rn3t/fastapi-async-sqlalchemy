# tests/test_multi_session_fixes.py
"""Regression tests for multi-sessions bug fixes."""

import asyncio
import sys

import pytest
from sqlalchemy import text

db_url = "sqlite+aiosqlite://"


def _ensure_modules():
    for mod_name in ("fastapi_async_sqlalchemy", "fastapi_async_sqlalchemy.middleware"):
        if mod_name not in sys.modules:
            __import__(mod_name)


def _make_middleware_and_db(**engine_kw):
    _ensure_modules()
    from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

    Middleware, _db = create_middleware_and_session_proxy()
    Middleware(app=None, db_url=db_url, engine_args=engine_kw)
    return _db


def _get_ctx_var(_db, var_name: str):
    """Extract a ContextVar from the DBSessionMeta.session property closure."""
    session_prop = type(_db).__dict__["session"]
    closure = {
        name: cell.cell_contents
        for name, cell in zip(
            session_prop.fget.__code__.co_freevars,
            session_prop.fget.__closure__,
        )
    }
    return closure[var_name]


# ---------------------------------------------------------------------------
# Fix 1: call_soon race — cleanup_tasks must be populated before first await
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_tasks_populated_before_aexit_gathers():
    """
    Regression: cleanup_tasks must be populated synchronously when done_callbacks fire,
    NOT deferred via call_soon (which runs on the next event loop iteration).

    Before fix: state.cleanup_tasks is not fully populated immediately after
    asyncio.gather(*tasks) returns because call_soon hasn't run yet.
    After fix: create_task() is called directly in done_callback, so cleanup_tasks
    is fully populated when gather(*tasks) returns.
    """
    _db = _make_middleware_and_db()
    multi_state_var = _get_ctx_var(_db, "_multi_state")

    async with _db(multi_sessions=True):

        async def work(n):
            _ = _db.session  # registers done_callback on current task
            return n

        tasks = [asyncio.create_task(work(i)) for i in range(3)]
        await asyncio.gather(*tasks)

        # Do NOT yield here — check synchronously that cleanup_tasks is populated.
        # With the call_soon bug: len < 3 (some/all schedule_cleanup callbacks haven't run yet).
        # After fix: len == 3 (create_task called directly in done_callback, synchronously).
        state = multi_state_var.get()
        assert state is not None
        assert len(state.cleanup_tasks) == 3, (
            f"Expected 3 cleanup tasks after gather returns (got {len(state.cleanup_tasks)}). "
            "Still using call_soon which defers task creation to a future event loop tick."
        )
