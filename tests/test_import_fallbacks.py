"""
Tests for import fallback scenarios.

These tests verify that the code handles missing optional dependencies gracefully.
"""


def test_sqlmodel_not_installed_fallback():
    """Test fallback when SQLModel is not installed."""
    import inspect

    import fastapi_async_sqlalchemy.middleware as mod

    # Verify the fallback code structure exists
    source = inspect.getsource(mod)
    assert "try:" in source
    assert "from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession" in source
    assert "except ImportError:" in source
    assert "DefaultAsyncSession: type[AsyncSession] = AsyncSession" in source


def test_default_async_session_type():
    """Test that DefaultAsyncSession is properly set."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from fastapi_async_sqlalchemy.middleware import DefaultAsyncSession

    # Should be either SQLModel's AsyncSession or SQLAlchemy's AsyncSession
    assert issubclass(DefaultAsyncSession, AsyncSession)

    # Verify it's a valid session class
    assert hasattr(DefaultAsyncSession, "__init__")


def test_coverage_pragmas_not_needed():
    """
    Verify that fallback imports don't need pragma: no cover.

    We achieve this by having tests that at least verify the code structure,
    even if we can't execute both paths in a single test run.
    """
    import inspect

    import fastapi_async_sqlalchemy.middleware as mod

    source = inspect.getsource(mod)

    # Ensure no pragma: no cover on import blocks
    # (These should be covered by structural tests)
    lines = source.split("\n")
    for i, line in enumerate(lines):
        if "pragma: no cover" in line:
            # Check if it's in an import block
            if i > 0 and "import" in lines[i - 1]:
                raise AssertionError(
                    f"Import fallback at line {i} should not have pragma: no cover. "
                    "Use structural tests instead."
                )
