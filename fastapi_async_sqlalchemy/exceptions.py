from __future__ import annotations

from typing import Any


class MissingSessionError(Exception):
    """
    Exception raised for when the user tries to access a database session before it is created.
    """

    def __init__(self):
        msg = """
        No session found! Either you are not currently in a request context,
        or you need to manually create a session context by using a `db` instance as
        a context manager e.g.:

        async with db():
            await db.session.execute(foo.select()).fetchall()
        """

        super().__init__(msg)


class SessionNotInitialisedError(Exception):
    """
    Exception raised when the user creates a new DB session without first initialising it.
    """

    def __init__(self):
        msg = """
        Session not initialised! Ensure that DBSessionMiddleware has been initialised before
        attempting database access.
        """

        super().__init__(msg)


class PoolTimeoutError(TimeoutError):
    """Raised when a connection could not be checked out within the configured deadline.

    This is what ``db(pool_timeout=...)`` and ``db.connection(timeout=...)`` raise
    instead of parking on the engine-wide ``pool_timeout`` (30s by default, often
    raised to 60s in production). It subclasses the builtin :class:`TimeoutError`,
    so existing ``except TimeoutError`` handlers keep working.

    Map it to a ``503`` with a ``Retry-After`` header so callers can back off
    instead of receiving an opaque ``500``::

        @app.exception_handler(PoolTimeoutError)
        async def pool_timeout_handler(request, exc: PoolTimeoutError):
            return JSONResponse(
                {"detail": "database connection pool exhausted"},
                status_code=503,
                headers={"Retry-After": str(exc.retry_after)},
            )

    Attributes:
        timeout: The deadline, in seconds, that was exceeded.
        retry_after: Suggested ``Retry-After`` value, in whole seconds.
        pool_status: Snapshot of the pool at failure time (same shape as
            ``db.pool_status()``), or ``None`` when it could not be read.
    """

    def __init__(
        self,
        timeout: float | None = None,
        retry_after: int = 1,
        pool_status: dict[str, Any] | None = None,
    ):
        self.timeout = timeout
        self.retry_after = retry_after
        self.pool_status = pool_status

        deadline = "the configured deadline" if timeout is None else f"{timeout}s"
        msg = f"Could not check out a database connection within {deadline}."
        if pool_status:
            msg += (
                f" Pool status: {pool_status.get('checked_out')}/"
                f"{pool_status.get('capacity')} connections checked out, "
                f"{pool_status.get('available')} available."
            )
        msg += (
            " The connection pool is saturated — either the consumers of this service "
            "outnumber its pool capacity, or requests are holding connections for too long."
        )

        super().__init__(msg)
