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


def create_middleware_and_session_proxy():
    _Session: Optional[async_sessionmaker] = None
    _session: ContextVar[Optional[AsyncSession]] = ContextVar("_session", default=None)
    _multi_sessions_ctx: ContextVar[bool] = ContextVar("_multi_sessions_context", default=False)

    class SQLAlchemyMiddleware(BaseHTTPMiddleware):
        def __init__(
            self,
            app: ASGIApp,
            db_url: Optional[Union[str, URL]] = None,
            custom_engine: Optional[Engine] = None,
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
            if _Session is None:
                raise SessionNotInitialisedError

            current_session = _session.get()
            if current_session is None:
                raise MissingSessionError

            multi_sessions = _multi_sessions_ctx.get()
            if multi_sessions:
                task = asyncio.current_task()
                if not hasattr(task, "_db_session"):
                    task._db_session = _Session()

                    def cleanup(future):
                        session = getattr(task, "_db_session", None)
                        if session:

                            async def do_cleanup():
                                try:
                                    if future.exception():
                                        await session.rollback()
                                    else:
                                        await session.commit()
                                finally:
                                    await session.close()

                            asyncio.create_task(do_cleanup())

                    task.add_done_callback(cleanup)
                return task._db_session
            else:
                return current_session

        def __call__(
            cls,
            session_args: Optional[Dict] = None,
            commit_on_exit: bool = False,
            multi_sessions: bool = False,
        ):
            return cls._context_manager(
                session_args=session_args,
                commit_on_exit=commit_on_exit,
                multi_sessions=multi_sessions,
            )

        def _context_manager(
            cls,
            session_args: Dict = None,
            commit_on_exit: bool = False,
            multi_sessions: bool = False,
        ):
            return DBSessionContextManager(
                session_args=session_args,
                commit_on_exit=commit_on_exit,
                multi_sessions=multi_sessions,
            )

    class DBSession(metaclass=DBSessionMeta):
        pass

    class DBSessionContextManager:
        def __init__(
            self,
            session_args: Dict = None,
            commit_on_exit: bool = False,
            multi_sessions: bool = False,
        ):
            self.session_args = session_args or {}
            self.commit_on_exit = commit_on_exit
            self.multi_sessions = multi_sessions
            self.token = None
            self.multi_sessions_token = None
            self._session = None

        async def __aenter__(self):
            if _Session is None:
                raise SessionNotInitialisedError

            if self.multi_sessions:
                self.multi_sessions_token = _multi_sessions_ctx.set(True)

            self._session = _Session(**self.session_args)
            self.token = _session.set(self._session)
            return self

        @property
        def session(self):
            return self._session

        async def __aexit__(self, exc_type, exc_value, traceback):
            session = _session.get()

            try:
                if exc_type is not None:
                    await session.rollback()
                elif self.commit_on_exit:
                    await session.commit()
            finally:
                await session.close()
                _session.reset(self.token)
                if self.multi_sessions_token is not None:
                    _multi_sessions_ctx.reset(self.multi_sessions_token)

    return SQLAlchemyMiddleware, DBSession


SQLAlchemyMiddleware, db = create_middleware_and_session_proxy()
