from __future__ import annotations

import asyncio
import warnings
from contextvars import ContextVar
from dataclasses import dataclass, field

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
    _Session: async_sessionmaker | None = None
    _session: ContextVar[AsyncSession | None] = ContextVar("_session", default=None)
    _multi_sessions_ctx: ContextVar[bool] = ContextVar("_multi_sessions_context", default=False)
    _multi_state: ContextVar[_MultiSessionState | None] = ContextVar(
        "_multi_sessions_state",
        default=None,
    )

    @dataclass
    class _MultiSessionState:
        tracked: set[AsyncSession] = field(default_factory=set)
        task_sessions: dict[int, AsyncSession] = field(default_factory=dict)
        cleanup_tasks: list[asyncio.Task] = field(default_factory=list)
        parent_task_id: int | None = None
        commit_on_exit: bool = False
        session_args: dict = field(default_factory=dict)

    async def _finalize_session(
        session: AsyncSession,
        commit_on_exit: bool,
        exc: BaseException | None,
    ) -> None:
        try:
            # Check if session is already closed or inactive to prevent InvalidRequestError
            if not session.is_active:
                return

            if exc is not None:
                await session.rollback()
            elif commit_on_exit:
                try:
                    await session.commit()
                except Exception as commit_error:
                    try:
                        await session.rollback()
                    except Exception as rollback_error:
                        warnings.warn(
                            f"Rollback failed during cleanup: {rollback_error}",
                            stacklevel=2,
                        )
                    warnings.warn(
                        f"Commit failed during cleanup: {commit_error}",
                        stacklevel=2,
                    )
        except Exception:
            # If rollback or commit fails (e.g. session in invalid state),
            # we should still try to close it
            pass
        finally:
            try:
                await session.close()
            except Exception as close_error:
                warnings.warn(
                    f"Close failed during cleanup: {close_error}",
                    stacklevel=2,
                )

    class _SQLAlchemyMiddleware(BaseHTTPMiddleware):
        __test__ = False

        def __init__(
            self,
            app: ASGIApp,
            db_url: str | URL | None = None,
            custom_engine: AsyncEngine | None = None,
            engine_args: dict | None = None,
            session_args: dict | None = None,
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
                state = _multi_state.get()
                if state is None:
                    warnings.warn("Multi-session state is not initialized", stacklevel=2)
                    return _Session()

                task = asyncio.current_task()
                task_id = id(task) if task is not None else None
                print(f"DEBUG: db.session requested by task {task_id}")

                if task_id is not None and task_id in state.task_sessions:
                    print(f"DEBUG: Returning existing session for task {task_id}")
                    return state.task_sessions[task_id]

                session = _Session(**state.session_args)
                print(f"DEBUG: Created new session {id(session)} for task {task_id}")
                state.tracked.add(session)
                if task_id is not None:
                    state.task_sessions[task_id] = session

                # Capture loop from current context
                try:
                    current_loop = asyncio.get_running_loop()
                except RuntimeError:
                    current_loop = None

                def cleanup_callback(finished_task: asyncio.Task) -> None:
                    async def cleanup() -> None:
                        # Check if session is still tracked (not cleaned up by __aexit__)
                        # This check and discard is atomic in asyncio
                        if session not in state.tracked:
                            return
                        state.tracked.discard(session)

                        task_exception: BaseException | None
                        try:
                            task_exception = finished_task.exception()
                        except (asyncio.CancelledError, GeneratorExit) as e:
                            # Handle cancellation and GeneratorExit explicitly
                            task_exception = e
                        except BaseException as error:
                            task_exception = error
                        
                        await _finalize_session(
                            session,
                            commit_on_exit=state.commit_on_exit,
                            exc=task_exception,
                        )
                        
                        if task_id is not None:
                            state.task_sessions.pop(task_id, None)

                    if current_loop and not current_loop.is_closed():
                        current_loop.call_soon(lambda: state.cleanup_tasks.append(asyncio.create_task(cleanup())))
                    else:
                        warnings.warn("No running event loop during cleanup", stacklevel=2)

                # if task is not None and task_id != state.parent_task_id:
                #     task.add_done_callback(cleanup_callback)
                return session
            else:
                session = _session.get()
                if session is None:
                    raise MissingSessionError
                return session

    class DBSession(metaclass=DBSessionMeta):
        def __init__(
            self,
            session_args: dict | None = None,
            commit_on_exit: bool = False,
            multi_sessions: bool = False,
        ):
            self.token = None
            self.multi_sessions_token = None
            self.multi_state_token = None
            self.session_args = session_args or {}
            self.commit_on_exit = commit_on_exit
            self.multi_sessions = multi_sessions

        async def __aenter__(self):
            if not isinstance(_Session, async_sessionmaker):
                raise SessionNotInitialisedError

            if self.multi_sessions:
                self.multi_sessions_token = _multi_sessions_ctx.set(True)
                parent_task = asyncio.current_task()
                self.multi_state_token = _multi_state.set(
                    _MultiSessionState(
                        parent_task_id=id(parent_task) if parent_task else None,
                        commit_on_exit=self.commit_on_exit,
                        session_args=self.session_args,
                    )
                )
            else:
                self.token = _session.set(_Session(**self.session_args))
            return type(self)

        async def __aexit__(self, exc_type, exc_value, traceback):
            if self.multi_sessions:
                print(f"__aexit__ starting multi_sessions cleanup. exc={exc_type}")
                _multi_sessions_ctx.reset(self.multi_sessions_token)
                state = _multi_state.get()
                if state is not None:
                    if state.cleanup_tasks:
                        print(f"__aexit__ waiting for {len(state.cleanup_tasks)} cleanup tasks")
                        await asyncio.gather(*state.cleanup_tasks, return_exceptions=True)
                        print("__aexit__ cleanup tasks finished")
                    exc = exc_value if exc_type is not None else None
                    # Claim all remaining sessions to prevent concurrent cleanup
                    sessions_to_finalize = list(state.tracked)
                    state.tracked.clear()
                    print(f"__aexit__ finalizing {len(sessions_to_finalize)} remaining sessions")

                    for session in sessions_to_finalize:
                        await _finalize_session(
                            session,
                            commit_on_exit=state.commit_on_exit,
                            exc=exc,
                        )
                _multi_state.reset(self.multi_state_token)
                print("__aexit__ finished")
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
