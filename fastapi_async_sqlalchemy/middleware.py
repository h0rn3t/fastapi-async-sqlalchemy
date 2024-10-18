import asyncio
from contextvars import ContextVar
from typing import Dict, Optional, Union

from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.types import ASGIApp

from fastapi_async_sqlalchemy.exceptions import MissingSessionError, SessionNotInitialisedError

try:
    from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: F811
except ImportError:
    from sqlalchemy.orm import sessionmaker as async_sessionmaker


def create_middleware_and_session_proxy():
    _Session: Optional[async_sessionmaker] = None
    _session: ContextVar[Optional[AsyncSession]] = ContextVar("_session", default=None)
    _multi_sessions_ctx: ContextVar[bool] = ContextVar("_multi_sessions_context", default=False)
    _commit_on_exit_ctx: ContextVar[bool] = ContextVar("_commit_on_exit_ctx", default=False)
    # Usage of context vars inside closures is not recommended, since they are not properly
    # garbage collected, but in our use case context var is created on program startup and
    # is used throughout the whole its lifecycle.

    class SQLAlchemyMiddleware(BaseHTTPMiddleware):
        def __init__(
            self,
            app: ASGIApp,
            db_url: Optional[Union[str, URL]] = None,
            custom_engine: Optional[Engine] = None,
            engine_args: Dict = None,
            session_args: Dict = None,
            commit_on_exit: bool = False,
        ):
            super().__init__(app)
            self.commit_on_exit = commit_on_exit
            engine_args = engine_args or {}
            session_args = session_args or {}

            if not custom_engine and not db_url:
                raise ValueError("You need to pass a db_url or a custom_engine parameter.")
            if not custom_engine:
                engine = create_async_engine(db_url, **engine_args)
            else:
                engine = custom_engine

            nonlocal _Session
            _Session = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False, **session_args
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
                commit_on_exit = _commit_on_exit_ctx.get()
                # Always create a new session for each access when multi_sessions=True
                session = _Session()

                async def cleanup():
                    try:
                        if commit_on_exit:
                            await session.commit()
                    except Exception:
                        await session.rollback()
                        raise
                    finally:
                        await session.close()

                task = asyncio.current_task()
                if task is not None:
                    task.add_done_callback(lambda t: asyncio.create_task(cleanup()))
                return session
            else:
                session = _session.get()
                if session is None:
                    raise MissingSessionError
                return session

    class DBSession(metaclass=DBSessionMeta):
        def __init__(
            self,
            session_args: Dict = None,
            commit_on_exit: bool = False,
            multi_sessions: bool = False,
        ):
            self.token = None
            self.commit_on_exit_token = None
            self.session_args = session_args or {}
            self.commit_on_exit = commit_on_exit
            self.multi_sessions = multi_sessions

        async def __aenter__(self):
            if not isinstance(_Session, async_sessionmaker):
                raise SessionNotInitialisedError

            if self.multi_sessions:
                self.multi_sessions_token = _multi_sessions_ctx.set(True)
                self.commit_on_exit_token = _commit_on_exit_ctx.set(self.commit_on_exit)
            else:
                self.token = _session.set(_Session(**self.session_args))
            return type(self)

        async def __aexit__(self, exc_type, exc_value, traceback):
            if self.multi_sessions:
                _multi_sessions_ctx.reset(self.multi_sessions_token)
                _commit_on_exit_ctx.reset(self.commit_on_exit_token)
            else:
                session = _session.get()
                try:
                    if exc_type is not None:
                        await session.rollback()
                    elif self.commit_on_exit:
                        await session.commit()
                finally:
                    await session.close()
                    _session.reset(self.token)

    return SQLAlchemyMiddleware, DBSession


SQLAlchemyMiddleware, db = create_middleware_and_session_proxy()
