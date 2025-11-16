import warnings
from contextvars import ContextVar
from typing import Dict, Optional, Set, Type, Union

from sqlalchemy.engine.url import URL
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.types import ASGIApp

from fastapi_async_sqlalchemy.exceptions import (
    MissingSessionError,
    SessionNotInitialisedError,
)

try:
    from sqlalchemy.ext.asyncio import async_sessionmaker
except ImportError:
    from sqlalchemy.orm import sessionmaker as async_sessionmaker  # type: ignore

# Try to import SQLModel's AsyncSession which has the .exec() method
try:
    from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

    DefaultAsyncSession: Type[AsyncSession] = SQLModelAsyncSession  # type: ignore
except ImportError:
    DefaultAsyncSession: Type[AsyncSession] = AsyncSession  # type: ignore


def create_middleware_and_session_proxy() -> tuple:
    _Session: Optional[async_sessionmaker] = None
    _session: ContextVar[Optional[AsyncSession]] = ContextVar("_session", default=None)
    _multi_sessions_ctx: ContextVar[bool] = ContextVar("_multi_sessions_context", default=False)
    _commit_on_exit_ctx: ContextVar[bool] = ContextVar("_commit_on_exit_ctx", default=False)
    _tracked_sessions: ContextVar[Optional[Set[AsyncSession]]] = ContextVar(
        "_tracked_sessions", default=None
    )
    # Usage of context vars inside closures is not recommended, since they are not properly
    # garbage collected, but in our use case context var is created on program startup and
    # is used throughout the whole its lifecycle.

    class SQLAlchemyMiddleware(BaseHTTPMiddleware):
        def __init__(
            self,
            app: ASGIApp,
            db_url: Optional[Union[str, URL]] = None,
            custom_engine: Optional[AsyncEngine] = None,
            engine_args: Optional[Dict] = None,
            session_args: Optional[Dict] = None,
            commit_on_exit: bool = False,
        ):
            super().__init__(app)
            self.commit_on_exit = commit_on_exit
            engine_args = engine_args or {}
            session_args = session_args or {}

            if not custom_engine and not db_url:
                raise ValueError("You need to pass a db_url or a custom_engine parameter.")
            if not custom_engine:
                if db_url is None:
                    raise ValueError("db_url cannot be None when custom_engine is not provided")
                engine = create_async_engine(db_url, **engine_args)
            else:
                engine = custom_engine

            nonlocal _Session
            _Session = async_sessionmaker(
                engine,
                class_=DefaultAsyncSession,
                expire_on_commit=False,
                **session_args,
            )

        async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
            async with DBSession(commit_on_exit=self.commit_on_exit):
                return await call_next(request)

    class DBSessionMeta(type):
        @property
        def session(self) -> AsyncSession:
            """Return an instance of Session local to the current async context."""
            if _Session is None:
                raise SessionNotInitialisedError

            multi_sessions = _multi_sessions_ctx.get()
            if multi_sessions:
                """In this case, we need to create a new session for each task.
                We also need to commit the session on exit if commit_on_exit is True.
                This is useful when we need to run multiple queries in parallel.
                For example, when we need to run multiple queries in parallel in a route handler.
                Example:
                ```python
                    async with db(multi_sessions=True):
                        async def execute_query(query):
                            return await db.session.execute(text(query))

                        tasks = [
                            asyncio.create_task(execute_query("SELECT 1")),
                            asyncio.create_task(execute_query("SELECT 2")),
                            asyncio.create_task(execute_query("SELECT 3")),
                            asyncio.create_task(execute_query("SELECT 4")),
                            asyncio.create_task(execute_query("SELECT 5")),
                            asyncio.create_task(execute_query("SELECT 6")),
                        ]

                        await asyncio.gather(*tasks)
                ```
                """
                # Always create a new session for each access when multi_sessions=True
                session = _Session()

                # Track the session for cleanup in __aexit__
                tracked = _tracked_sessions.get()
                if tracked is not None:
                    tracked.add(session)
                else:
                    warnings.warn(
                        """Session created in multi_sessions mode but no tracking set found.
                        This session may leak if not properly closed.""",
                        ResourceWarning,
                        stacklevel=2,
                    )

                return session
            else:
                session = _session.get()
                if session is None:
                    raise MissingSessionError
                return session

    class DBSession(metaclass=DBSessionMeta):
        def __init__(
            self,
            session_args: Optional[Dict] = None,
            commit_on_exit: bool = False,
            multi_sessions: bool = False,
        ):
            self.token = None
            self.commit_on_exit_token = None
            self.tracked_sessions_token = None
            self.session_args = session_args or {}
            self.commit_on_exit = commit_on_exit
            self.multi_sessions = multi_sessions

        async def __aenter__(self):
            if not isinstance(_Session, async_sessionmaker):
                raise SessionNotInitialisedError

            if self.multi_sessions:
                self.multi_sessions_token = _multi_sessions_ctx.set(True)
                self.commit_on_exit_token = _commit_on_exit_ctx.set(self.commit_on_exit)
                self.tracked_sessions_token = _tracked_sessions.set(set())
            else:
                self.token = _session.set(_Session(**self.session_args))
            return type(self)

        async def __aexit__(self, exc_type, exc_value, traceback):
            if self.multi_sessions:
                # Clean up all tracked sessions
                tracked_sessions = _tracked_sessions.get()
                if tracked_sessions:
                    cleanup_errors = []
                    for session in tracked_sessions:
                        try:
                            if exc_type is not None:
                                await session.rollback()
                            elif self.commit_on_exit:
                                try:
                                    await session.commit()
                                except Exception as commit_error:
                                    warnings.warn(
                                        f"Failed to commit in multi_sessions: {commit_error}",
                                        RuntimeWarning,
                                        stacklevel=2,
                                    )
                                    await session.rollback()
                                    cleanup_errors.append(commit_error)
                        except Exception as cleanup_error:
                            warnings.warn(
                                f"Failed to rollback session in multi_sessions: {cleanup_error}",
                                RuntimeWarning,
                                stacklevel=2,
                            )
                            cleanup_errors.append(cleanup_error)
                        finally:
                            try:
                                await session.close()
                            except Exception as close_error:
                                warnings.warn(
                                    f"Failed to close session in multi_session: {close_error}",
                                    ResourceWarning,
                                    stacklevel=2,
                                )
                                cleanup_errors.append(close_error)

                    if cleanup_errors and exc_type is None:
                        warnings.warn(
                            f"Encountered {len(cleanup_errors)} error(s) during session cleanup",
                            RuntimeWarning,
                            stacklevel=2,
                        )

                # Reset context vars
                _tracked_sessions.reset(self.tracked_sessions_token)
                _multi_sessions_ctx.reset(self.multi_sessions_token)
                _commit_on_exit_ctx.reset(self.commit_on_exit_token)
            else:
                # Standard single-session mode
                session = _session.get()
                try:
                    if exc_type is not None:
                        await session.rollback()
                    elif self.commit_on_exit:
                        try:
                            await session.commit()
                        except Exception:
                            await session.rollback()
                            raise
                finally:
                    await session.close()
                    _session.reset(self.token)

    return SQLAlchemyMiddleware, DBSession


SQLAlchemyMiddleware, db = create_middleware_and_session_proxy()
