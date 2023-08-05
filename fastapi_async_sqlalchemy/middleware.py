from contextvars import ContextVar
from typing import Dict, Optional, Union

from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.types import ASGIApp

from fastapi_async_sqlalchemy.exceptions import MissingSessionError, SessionNotInitialisedError

_Session: Optional[async_sessionmaker] = None
_session: ContextVar[Optional[AsyncSession]] = ContextVar("_session", default=None)


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

        expire_on_commit = session_args.pop("expire_on_commit", False)
        if expire_on_commit is True:
            raise ValueError("expire_on_commit must be False for async sessions.")

        global _Session
        _Session = async_sessionmaker(engine, expire_on_commit=expire_on_commit, **session_args)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        async with db(commit_on_exit=self.commit_on_exit):
            return await call_next(request)


class DBSessionMeta(type):
    # using this metaclass means that we can access db.session as a property at a class level,
    # rather than db().session
    @property
    def session(self) -> AsyncSession:
        """Return an instance of Session local to the current async context."""
        if _Session is None:
            raise SessionNotInitialisedError

        session = _session.get()
        if session is None:
            raise MissingSessionError

        return session


class DBSession(metaclass=DBSessionMeta):
    def __init__(self, session_args: Dict = None, commit_on_exit: bool = False):
        self.token = None
        self.session_args = session_args or {}
        self.commit_on_exit = commit_on_exit

    async def __aenter__(self):
        if not isinstance(_Session, async_sessionmaker):
            raise SessionNotInitialisedError

        self.token = _session.set(_Session(**self.session_args))  # type: ignore
        return type(self)

    async def __aexit__(self, exc_type, exc_value, traceback):
        session = _session.get()
        if exc_type is not None:
            await session.rollback()

        if self.commit_on_exit:
            await session.commit()

        await session.close()
        _session.reset(self.token)


db: DBSessionMeta = DBSession
