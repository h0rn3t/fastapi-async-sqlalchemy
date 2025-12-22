"""
Test import fallback when SQLModel is not available.

This test uses sys.meta_path to block SQLModel imports.
"""

import importlib.abc
import importlib.machinery
import sys


class BlockSQLModelFinder(importlib.abc.MetaPathFinder):
    """Meta path finder that blocks sqlmodel imports."""

    def find_spec(self, fullname, path, target=None):
        if "sqlmodel" in fullname:
            raise ImportError(f"No module named '{fullname}'")
        return None  # Let other finders handle this


def test_sqlmodel_import_fallback():
    """Test that middleware works without SQLModel installed."""
    # Save current state
    saved_modules = {}
    for key in list(sys.modules.keys()):
        if key.startswith("sqlmodel") or key == "fastapi_async_sqlalchemy.middleware":
            saved_modules[key] = sys.modules.pop(key, None)

    # Install the import blocker
    blocker = BlockSQLModelFinder()
    sys.meta_path.insert(0, blocker)

    try:
        # Import middleware fresh - this should trigger the ImportError path for SQLModel
        # Verify that DefaultAsyncSession falls back to plain AsyncSession
        from sqlalchemy.ext.asyncio import AsyncSession

        import fastapi_async_sqlalchemy.middleware as middleware

        assert middleware.DefaultAsyncSession is AsyncSession, (
            "Expected DefaultAsyncSession to be AsyncSession when SQLModel is not available"
        )

    finally:
        # Remove the blocker
        sys.meta_path.remove(blocker)

        # Clean up imported module
        sys.modules.pop("fastapi_async_sqlalchemy.middleware", None)
        sys.modules.pop("fastapi_async_sqlalchemy", None)

        # Restore saved modules
        for key, value in saved_modules.items():
            if value is not None:
                sys.modules[key] = value

        # Re-import to restore normal state
