import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
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


@dataclass(slots=True)
class MultiSessionState:
    """State for multi_sessions mode."""

    tracked: set[AsyncSession] = field(default_factory=set)
    task_sessions: dict[int, AsyncSession] = field(default_factory=dict)
    cleanup_tasks: list[asyncio.Task] = field(default_factory=list)
    parent_task_id: int = 0
    commit_on_exit: bool = False


def create_middleware_and_session_proxy() -> tuple:
    _Session: Optional[async_sessionmaker] = None
    _session: ContextVar[Optional[AsyncSession]] = ContextVar("_session", default=None)
    _multi_state: ContextVar[Optional[MultiSessionState]] = ContextVar("_multi_state", default=None)

    class _SQLAlchemyMiddleware(BaseHTTPMiddleware):
        __test__ = False  # Prevent pytest from collecting this as a test class

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

            state = _multi_state.get()
            if state is not None:
                task = asyncio.current_task()
                if task is None:
                    raise RuntimeError("Cannot get current task")
                task_id = id(task)

                if task_id in state.task_sessions:
                    return state.task_sessions[task_id]

                session = _Session()
                state.task_sessions[task_id] = session
                state.tracked.add(session)

                # Add cleanup callback only for child tasks
                if task_id != state.parent_task_id:

                    def cleanup_callback(_task):
                        try:
                            loop = asyncio.get_running_loop()
                            if loop.is_closed():
                                return
                        except RuntimeError:
                            return

                        async def cleanup():
                            try:
                                if state.commit_on_exit:
                                    try:
                                        await session.commit()
                                    except Exception:
                                        await session.rollback()
                            finally:
                                await session.close()
                                state.tracked.discard(session)
                                state.task_sessions.pop(task_id, None)

                        t = loop.create_task(cleanup())
                        state.cleanup_tasks.append(t)

                    task.add_done_callback(cleanup_callback)

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
            self.multi_state_token = None
            self.session_args = session_args or {}
            self.commit_on_exit = commit_on_exit
            self.multi_sessions = multi_sessions

        async def __aenter__(self):
            if not isinstance(_Session, async_sessionmaker):
                raise SessionNotInitialisedError

            if self.multi_sessions:
                state = MultiSessionState(
                    parent_task_id=id(asyncio.current_task()),
                    commit_on_exit=self.commit_on_exit,
                )
                self.multi_state_token = _multi_state.set(state)
            else:
                self.token = _session.set(_Session(**self.session_args))
            return type(self)

        async def __aexit__(self, exc_type, exc_value, traceback):
            if self.multi_sessions:
                state = _multi_state.get()

                # Wait for cleanup tasks
                if state.cleanup_tasks:
                    await asyncio.sleep(0)
                    await asyncio.gather(*state.cleanup_tasks, return_exceptions=True)

                # Clean up remaining sessions
                for session in list(state.tracked):
                    try:
                        if exc_type is not None:
                            await session.rollback()
                        elif self.commit_on_exit:
                            try:
                                await session.commit()
                            except Exception:
                                await session.rollback()
                    except Exception:
                        pass
                    finally:
                        try:
                            await session.close()
                        except Exception:
                            pass

                _multi_state.reset(self.multi_state_token)
            else:
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

    return _SQLAlchemyMiddleware, DBSession


SQLAlchemyMiddleware, db = create_middleware_and_session_proxy()
