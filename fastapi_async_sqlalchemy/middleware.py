import asyncio
from contextvars import ContextVar
from typing import Optional, Union

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

try:
    from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

    DefaultAsyncSession: type[AsyncSession] = SQLModelAsyncSession  # type: ignore
except ImportError:
    DefaultAsyncSession: type[AsyncSession] = AsyncSession  # type: ignore


def create_middleware_and_session_proxy() -> tuple:
    _Session: Optional[async_sessionmaker] = None
    _session: ContextVar[Optional[AsyncSession]] = ContextVar("_session", default=None)
    _multi_sessions_ctx: ContextVar[bool] = ContextVar("_multi_sessions_context", default=False)
    _commit_on_exit_ctx: ContextVar[bool] = ContextVar("_commit_on_exit_ctx", default=False)

    class _SQLAlchemyMiddleware(BaseHTTPMiddleware):
        __test__ = False

        def __init__(
            self,
            app: ASGIApp,
            db_url: Optional[Union[str, URL]] = None,
            custom_engine: Optional[AsyncEngine] = None,
            engine_args: Optional[dict] = None,
            session_args: Optional[dict] = None,
            commit_on_exit: bool = False,
        ):
            super().__init__(app)
            self.commit_on_exit = commit_on_exit
            engine_args = engine_args or {}
            session_args = session_args or {}

            if not custom_engine and not db_url:
                raise ValueError("You need to pass a db_url or a custom_engine parameter.")
            if custom_engine:
                engine = custom_engine
            else:
                engine = create_async_engine(db_url, **engine_args)

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
                commit_on_exit = _commit_on_exit_ctx.get()
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
            session_args: Optional[dict] = None,
            commit_on_exit: bool = False,
            multi_sessions: bool = False,
        ):
            self.token = None
            self.multi_sessions_token = None
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

    return _SQLAlchemyMiddleware, DBSession


SQLAlchemyMiddleware, db = create_middleware_and_session_proxy()
