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
        semaphore: asyncio.Semaphore | None = None
        slot_holders: set[int] = field(default_factory=set)

    def _cleanup_error(error: BaseException) -> str:
        return f"{type(error).__name__}: {error}"

    def _raise_cleanup_errors(errors: list[BaseException]) -> None:
        if not errors:
            return
        if len(errors) == 1:
            raise errors[0]

        details = "; ".join(_cleanup_error(error) for error in errors)
        raise RuntimeError(f"Session cleanup failed with {len(errors)} errors: {details}")

    async def _finalize_session(
        session: AsyncSession,
        commit_on_exit: bool,
        exc: BaseException | None,
    ) -> None:
        errors: list[BaseException] = []

        # Rollback/commit must surface errors to caller; otherwise writes can be lost silently.
        if session.is_active:
            if exc is not None:
                try:
                    await session.rollback()
                except BaseException as rollback_error:
                    errors.append(rollback_error)
            elif commit_on_exit:
                try:
                    await session.commit()
                except BaseException as commit_error:
                    errors.append(commit_error)
                    try:
                        await session.rollback()
                    except BaseException as rollback_error:
                        errors.append(rollback_error)

        try:
            await session.close()
        except BaseException as close_error:
            errors.append(close_error)

        _raise_cleanup_errors(errors)

    class _ConnectionContextManager:
        """Async context manager for throttled session access in multi_sessions mode.

        When used within ``db(multi_sessions=True, max_concurrent=N)``, this
        context manager awaits a semaphore slot before creating a session.
        When the block exits, the session is finalized (committed or rolled back)
        and the slot is released, allowing the next waiting task to proceed.

        This prevents ``TimeoutError: QueuePool limit ... reached`` by ensuring
        no more than *max_concurrent* sessions hold connections simultaneously.
        """

        __slots__ = ("_session", "_state", "_semaphore", "_owns_session", "_acquired_slot")

        def __init__(self) -> None:
            self._session: AsyncSession | None = None
            self._state: _MultiSessionState | None = None
            self._semaphore: asyncio.Semaphore | None = None
            self._owns_session: bool = False
            self._acquired_slot: bool = False

        async def __aenter__(self) -> AsyncSession:
            if _Session is None:
                raise SessionNotInitialisedError

            self._state = _multi_state.get()
            self._semaphore = self._state.semaphore if self._state else None

            multi_sessions = _multi_sessions_ctx.get()
            if multi_sessions and self._state is not None:
                task = asyncio.current_task()
                task_id = id(task) if task is not None else None

                # Reuse existing session for this task
                if task_id is not None and task_id in self._state.task_sessions:
                    self._session = self._state.task_sessions[task_id]
                    self._owns_session = False
                    return self._session

                # Acquire pool slot only when this context creates a new session.
                if self._semaphore:
                    await self._semaphore.acquire()
                    self._acquired_slot = True
                    if task_id is not None:
                        self._state.slot_holders.add(task_id)

                # Create new session — we own it and will close it in __aexit__
                try:
                    session = _Session(**self._state.session_args)
                except BaseException:
                    if self._acquired_slot and self._semaphore:
                        self._semaphore.release()
                        self._acquired_slot = False
                    if task_id is not None:
                        self._state.slot_holders.discard(task_id)
                    raise
                self._state.tracked.add(session)
                if task_id is not None:
                    self._state.task_sessions[task_id] = session
                self._session = session
                self._owns_session = True
                return session
            else:
                # Not in multi_sessions mode — return the context session
                session = _session.get()
                if session is None:
                    raise MissingSessionError
                self._session = session
                self._owns_session = False
                return session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            task = asyncio.current_task()
            task_id = id(task) if task else None
            try:
                if self._owns_session and self._session is not None and self._state is not None:
                    # Remove from state to prevent double-cleanup by done_callback
                    if task_id is not None:
                        self._state.task_sessions.pop(task_id, None)
                    self._state.tracked.discard(self._session)

                    await _finalize_session(
                        self._session,
                        commit_on_exit=self._state.commit_on_exit,
                        exc=exc_val if exc_type is not None else None,
                    )
            finally:
                if task_id is not None and self._state is not None:
                    self._state.slot_holders.discard(task_id)
                if self._acquired_slot and self._semaphore:
                    self._semaphore.release()

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
                    raise RuntimeError("Multi-session state is not initialized")

                task = asyncio.current_task()
                task_id = id(task) if task is not None else None

                if task_id is not None and task_id in state.task_sessions:
                    return state.task_sessions[task_id]

                if (
                    state.semaphore is not None
                    and task_id is not None
                    and task_id != state.parent_task_id
                    and task_id not in state.slot_holders
                ):
                    raise RuntimeError(
                        "When `max_concurrent` is set, child tasks must access DB via "
                        "`db.connection()` or `db.gather()`; direct `db.session` access "
                        "from child tasks is not throttled."
                    )

                session = _Session(**state.session_args)
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
                        task_exception: BaseException | None
                        try:
                            task_exception = finished_task.exception()
                        except (asyncio.CancelledError, GeneratorExit) as e:
                            task_exception = e
                        except BaseException as error:
                            task_exception = error

                        if task_exception is None:
                            # Close session on success to return connection to pool
                            try:
                                if session in state.tracked:
                                    state.tracked.discard(session)
                                    await _finalize_session(
                                        session,
                                        commit_on_exit=state.commit_on_exit,
                                        exc=None,
                                    )
                            finally:
                                if task_id is not None:
                                    state.task_sessions.pop(task_id, None)
                                    state.slot_holders.discard(task_id)
                            return

                        try:
                            if session not in state.tracked:
                                return
                            state.tracked.discard(session)
                            await _finalize_session(
                                session,
                                commit_on_exit=state.commit_on_exit,
                                exc=task_exception,
                            )
                        finally:
                            if task_id is not None:
                                state.task_sessions.pop(task_id, None)
                                state.slot_holders.discard(task_id)

                    if current_loop and not current_loop.is_closed():

                        def schedule_cleanup() -> None:
                            cleanup_task = current_loop.create_task(cleanup())
                            state.cleanup_tasks.append(cleanup_task)

                        current_loop.call_soon(schedule_cleanup)
                    else:
                        warnings.warn("No running event loop during cleanup", stacklevel=2)

                if task is not None and task_id != state.parent_task_id:
                    task.add_done_callback(cleanup_callback)
                return session
            else:
                session = _session.get()
                if session is None:
                    raise MissingSessionError
                return session

        def connection(cls) -> _ConnectionContextManager:
            """Return an async context manager that respects pool throttling.

            When ``max_concurrent`` is set on the enclosing ``db(...)`` context,
            ``connection()`` waits for a free semaphore slot before creating a
            session.  The session is automatically closed when the block exits,
            releasing the slot for the next waiting task.

            Usage::

                async with db(multi_sessions=True, max_concurrent=10):
                    async def work(n):
                        async with db.connection() as session:
                            return await session.execute(text(f"SELECT {n}"))
                    tasks = [asyncio.create_task(work(i)) for i in range(100)]
                    results = await asyncio.gather(*tasks)

            Without ``max_concurrent`` the method still works — it simply
            creates a session without throttling and cleans it up on exit.
            """
            return _ConnectionContextManager()

        async def gather(cls, *coros_or_futures, return_exceptions: bool = False):
            """Drop-in replacement for ``asyncio.gather`` with pool throttling.

            Each coroutine is wrapped so that it acquires a semaphore slot
            (and thus a session) before running, and releases it after.
            This guarantees that no more than ``max_concurrent`` connections
            are in use at any time.

            Usage::

                async with db(multi_sessions=True, max_concurrent=10):
                    results = await db.gather(
                        do_work(1), do_work(2), ..., do_work(100),
                    )

            When ``max_concurrent`` is not set, delegates directly to
            ``asyncio.gather`` with no extra overhead.
            """
            state = _multi_state.get()
            semaphore = state.semaphore if state else None

            if semaphore is None:
                return await asyncio.gather(
                    *coros_or_futures,
                    return_exceptions=return_exceptions,
                )

            async def _throttled(coro):
                async with _ConnectionContextManager():
                    return await coro

            return await asyncio.gather(
                *[_throttled(c) for c in coros_or_futures],
                return_exceptions=return_exceptions,
            )

    class DBSession(metaclass=DBSessionMeta):
        def __init__(
            self,
            session_args: dict | None = None,
            commit_on_exit: bool = False,
            multi_sessions: bool = False,
            max_concurrent: int | None = None,
        ):
            if max_concurrent is not None and max_concurrent < 1:
                raise ValueError("`max_concurrent` must be greater than 0.")

            self.token = None
            self.multi_sessions_token = None
            self.multi_state_token = None
            self.session_args = session_args or {}
            self.commit_on_exit = commit_on_exit
            self.multi_sessions = multi_sessions
            self.max_concurrent = max_concurrent

        async def __aenter__(self):
            if not isinstance(_Session, async_sessionmaker):
                raise SessionNotInitialisedError

            if self.multi_sessions:
                self.multi_sessions_token = _multi_sessions_ctx.set(True)
                parent_task = asyncio.current_task()
                semaphore = (
                    asyncio.Semaphore(self.max_concurrent)
                    if self.max_concurrent is not None
                    else None
                )
                self.multi_state_token = _multi_state.set(
                    _MultiSessionState(
                        parent_task_id=id(parent_task) if parent_task else None,
                        commit_on_exit=self.commit_on_exit,
                        session_args=self.session_args,
                        semaphore=semaphore,
                    )
                )
            else:
                self.token = _session.set(_Session(**self.session_args))
            return type(self)

        async def __aexit__(self, exc_type, exc_value, traceback):
            if self.multi_sessions:
                _multi_sessions_ctx.reset(self.multi_sessions_token)
                state = _multi_state.get()
                cleanup_errors: list[BaseException] = []
                if state is not None:
                    if state.cleanup_tasks:
                        cleanup_results = await asyncio.gather(
                            *state.cleanup_tasks,
                            return_exceptions=True,
                        )
                        cleanup_errors.extend(
                            result
                            for result in cleanup_results
                            if isinstance(result, BaseException)
                        )
                    exc = exc_value if exc_type is not None else None
                    # Claim all remaining sessions to prevent concurrent cleanup
                    sessions_to_finalize = list(state.tracked)
                    state.tracked.clear()

                    for session in sessions_to_finalize:
                        try:
                            await _finalize_session(
                                session,
                                commit_on_exit=state.commit_on_exit,
                                exc=exc,
                            )
                        except BaseException as caught_cleanup_error:
                            cleanup_errors.append(caught_cleanup_error)
                _multi_state.reset(self.multi_state_token)

                if cleanup_errors:
                    if exc_type is None:
                        _raise_cleanup_errors(cleanup_errors)
                    for cleanup_exc in cleanup_errors:
                        warnings.warn(
                            "Suppressed session cleanup error because another exception is already "
                            f"being raised: {_cleanup_error(cleanup_exc)}",
                            stacklevel=2,
                        )
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
