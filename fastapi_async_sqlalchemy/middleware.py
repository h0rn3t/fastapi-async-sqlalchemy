from __future__ import annotations

import asyncio
import logging
import warnings
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from sqlalchemy.engine.url import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapi_async_sqlalchemy.exceptions import (
    MissingSessionError,
    SessionNotInitialisedError,
)

if TYPE_CHECKING:
    # Imported under an alias to avoid colliding with the closure-local
    # `DBSessionMeta` metaclass defined inside ``create_middleware_and_session_proxy``.
    from fastapi_async_sqlalchemy._types import DBSessionMeta as _DBSessionMetaProtocol

try:
    from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

    DefaultAsyncSession: type[AsyncSession] = SQLModelAsyncSession  # type: ignore
except ImportError:
    DefaultAsyncSession: type[AsyncSession] = AsyncSession  # type: ignore


def create_middleware_and_session_proxy() -> tuple[type, _DBSessionMetaProtocol]:
    _Session: async_sessionmaker | None = None
    _Session_engine: AsyncEngine | None = None
    _session: ContextVar[AsyncSession | None] = ContextVar("_session", default=None)
    _request_session: ContextVar[AsyncSession | None] = ContextVar(
        "_request_session",
        default=None,
    )
    _request_session_used: ContextVar[bool] = ContextVar(
        "_request_session_used",
        default=False,
    )
    _request_session_closed_for_streaming: ContextVar[bool] = ContextVar(
        "_request_session_closed_for_streaming",
        default=False,
    )
    _multi_sessions_ctx: ContextVar[bool] = ContextVar("_multi_sessions_context", default=False)
    _multi_state: ContextVar[_MultiSessionState | None] = ContextVar(
        "_multi_sessions_state",
        default=None,
    )

    @dataclass(slots=True)
    class _MultiSessionState:
        tracked: set[AsyncSession] = field(default_factory=set)
        task_sessions: dict[asyncio.Task, AsyncSession] = field(default_factory=dict)
        cleanup_tasks: list[asyncio.Task] = field(default_factory=list)
        parent_task: asyncio.Task | None = None
        commit_on_exit: bool = False
        session_args: dict = field(default_factory=dict)
        semaphore: asyncio.Semaphore | None = None
        slot_holders: set[asyncio.Task] = field(default_factory=set)
        waiters: set[asyncio.Task] = field(default_factory=set)
        closing: bool = False

    def _cleanup_error(error: BaseException) -> str:
        return f"{type(error).__name__}: {error}"

    def _raise_cleanup_errors(errors: list[BaseException]) -> None:
        if not errors:
            return
        if len(errors) == 1:
            raise errors[0]

        details = "; ".join(_cleanup_error(error) for error in errors)
        raise RuntimeError(f"Session cleanup failed with {len(errors)} errors: {details}")

    def _mark_request_session_used(session: AsyncSession) -> None:
        if session is not _request_session.get():
            return

        if _request_session_closed_for_streaming.get():
            raise RuntimeError(
                "The middleware-managed request database session is closed for streaming "
                "response body generation. Use `async with db()` inside the streaming "
                "generator to make the session lifetime explicit."
            )

        _request_session_used.set(True)

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
                if self._state.closing:
                    raise RuntimeError(
                        "Cannot create a db.connection() session after the owning "
                        "multi-session context has started closing."
                    )

                task = asyncio.current_task()

                # Reuse existing session for this task
                if task is not None and task in self._state.task_sessions:
                    self._session = self._state.task_sessions[task]
                    self._owns_session = False
                    return self._session

                # Acquire pool slot only when this context creates a new session.
                if self._semaphore:
                    if task is not None:
                        self._state.waiters.add(task)
                    try:
                        await self._semaphore.acquire()
                    finally:
                        if task is not None:
                            self._state.waiters.discard(task)

                    # Re-check closing: parent may have started shutdown while we
                    # were parked on acquire(). If so, release the slot we just
                    # took so it doesn't leak, and refuse to create a session.
                    if self._state.closing:
                        self._semaphore.release()
                        raise RuntimeError(
                            "Cannot create a db.connection() session after the owning "
                            "multi-session context has started closing."
                        )

                    self._acquired_slot = True
                    if task is not None:
                        self._state.slot_holders.add(task)

                # Create new session — we own it and will close it in __aexit__
                try:
                    session = _Session(**self._state.session_args)
                except BaseException:
                    if self._acquired_slot and self._semaphore:
                        self._semaphore.release()
                        self._acquired_slot = False
                    if task is not None:
                        self._state.slot_holders.discard(task)
                    raise
                self._state.tracked.add(session)
                if task is not None:
                    self._state.task_sessions[task] = session
                self._session = session
                self._owns_session = True
                return session
            else:
                # Not in multi_sessions mode — return the context session
                session = _session.get()
                if session is None:
                    raise MissingSessionError
                _mark_request_session_used(session)
                self._session = session
                self._owns_session = False
                return session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            task = asyncio.current_task()
            try:
                if self._owns_session and self._session is not None and self._state is not None:
                    # Remove from state to prevent double-cleanup by done_callback
                    if task is not None:
                        self._state.task_sessions.pop(task, None)
                    self._state.tracked.discard(self._session)

                    await _finalize_session(
                        self._session,
                        commit_on_exit=self._state.commit_on_exit,
                        exc=exc_val if exc_type is not None else None,
                    )
            finally:
                if task is not None and self._state is not None:
                    self._state.slot_holders.discard(task)
                if self._acquired_slot and self._semaphore:
                    self._semaphore.release()

    class _SQLAlchemyMiddleware:
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
            # Pure ASGI middleware: normal responses are buffered until the
            # request session finalizes, while streaming bodies can opt into an
            # explicit body-lifetime session with ``async with db()``.
            self.app = app
            self.commit_on_exit = commit_on_exit
            self.engine: AsyncEngine
            self.engine_owned = custom_engine is None
            self._engine_disposed = False
            engine_args = engine_args or {}
            session_args = session_args or {}

            if not custom_engine and not db_url:
                raise ValueError("You need to pass a db_url or a custom_engine parameter.")

            nonlocal _Session, _Session_engine

            # Validate the proxy/engine relationship BEFORE allocating a new
            # engine from `db_url`. Any engine we create here cannot be `is`
            # to an already-bound `_Session_engine`, so a rejected init must
            # not leak a freshly-created engine.
            if _Session_engine is not None and (
                custom_engine is None or _Session_engine is not custom_engine
            ):
                raise RuntimeError(
                    "This SQLAlchemy session proxy is already bound to another live engine. "
                    "Use create_middleware_and_session_proxy() for independent apps or "
                    "databases."
                )

            if custom_engine:
                engine = custom_engine
            else:
                assert db_url is not None
                engine = create_async_engine(db_url, **engine_args)

            self.engine = engine
            _Session_engine = engine
            _Session = async_sessionmaker(
                engine,
                class_=DefaultAsyncSession,
                expire_on_commit=False,
                **session_args,
            )

        async def dispose(self) -> None:
            if not self.engine_owned or self._engine_disposed:
                return

            nonlocal _Session, _Session_engine
            try:
                await self.engine.dispose()
                self._engine_disposed = True
            finally:
                # Always clear proxy bindings owned by this middleware. On
                # failure, this lets a retry actually re-attempt disposal
                # rather than silently no-op'ing on a half-disposed engine.
                if _Session_engine is self.engine:
                    _Session_engine = None
                    _Session = None

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "lifespan":

                async def send_with_disposal(message):
                    if message["type"] in (
                        "lifespan.shutdown.complete",
                        "lifespan.shutdown.failed",
                        "lifespan.startup.failed",
                    ):
                        captured: BaseException | None = None
                        try:
                            await self.dispose()
                        except Exception as disposal_exc:
                            captured = disposal_exc
                        finally:
                            # Forward the lifespan ack even if disposal raised,
                            # so the ASGI server is not left hanging.
                            await send(message)
                        if captured is not None:
                            logging.getLogger(__name__).warning(
                                "Engine disposal failed during ASGI lifespan %s",
                                message["type"],
                                exc_info=captured,
                            )
                            raise captured
                    else:
                        await send(message)

                await self.app(scope, receive, send_with_disposal)
                return

            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return

            request_context = DBSession(
                commit_on_exit=self.commit_on_exit,
                _request_context=True,
            )
            buffered_messages: list[Message] = []
            streaming_passthrough = False

            async def send_with_db_finalization(message: Message) -> None:
                nonlocal streaming_passthrough

                if streaming_passthrough:
                    await send(message)
                    return

                if message["type"] == "http.response.start":
                    buffered_messages.append(message)
                    return

                if message["type"] == "http.response.body":
                    if message.get("more_body", False):
                        if self.commit_on_exit and _request_session_used.get():
                            raise RuntimeError(
                                "`commit_on_exit=True` cannot use the middleware-managed "
                                "request database session with a streaming response. Use "
                                "`async with db()` inside the streaming generator, or manage "
                                "the streaming transaction explicitly."
                            )

                        if not _request_session_used.get():
                            await request_context.close_request_session_for_streaming()

                        for buffered_message in buffered_messages:
                            await send(buffered_message)
                        buffered_messages.clear()
                        await send(message)
                        streaming_passthrough = True
                        return

                    buffered_messages.append(message)
                    return

                buffered_messages.append(message)

            await request_context.__aenter__()
            try:
                await self.app(scope, receive, send_with_db_finalization)
            except BaseException as exc:
                await request_context.__aexit__(type(exc), exc, exc.__traceback__)
                raise

            await request_context.__aexit__(None, None, None)

            for message in buffered_messages:
                await send(message)

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
                if state.closing:
                    raise RuntimeError(
                        "Cannot create or access db.session after the owning multi-session "
                        "context has started closing."
                    )

                task = asyncio.current_task()

                if task is not None and task in state.task_sessions:
                    return state.task_sessions[task]

                if (
                    state.semaphore is not None
                    and task is not None
                    and task is not state.parent_task
                    and task not in state.slot_holders
                ):
                    raise RuntimeError(
                        "When `max_concurrent` is set, child tasks must access DB via "
                        "`db.connection()` or `db.gather()`; direct `db.session` access "
                        "from child tasks is not throttled."
                    )

                session = _Session(**state.session_args)
                state.tracked.add(session)
                if task is not None:
                    state.task_sessions[task] = session

                # Capture loop from current context
                try:
                    current_loop = asyncio.get_running_loop()
                except RuntimeError:
                    current_loop = None

                def cleanup_callback(finished_task: asyncio.Task) -> None:
                    async def cleanup() -> None:
                        # Invariant: do NOT add `await` between the
                        # `session in state.tracked` check and the matching
                        # `state.tracked.discard(session)` below. The sweep in
                        # DBSession.__aexit__ runs `list(state.tracked);
                        # state.tracked.clear()` atomically; an await here
                        # would let the sweep claim the same session and
                        # cause double-finalization.
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
                                if task is not None:
                                    state.task_sessions.pop(task, None)
                                    state.slot_holders.discard(task)
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
                            if task is not None:
                                state.task_sessions.pop(task, None)
                                state.slot_holders.discard(task)

                    if current_loop and not current_loop.is_closed():
                        cleanup_task = current_loop.create_task(cleanup())
                        state.cleanup_tasks.append(cleanup_task)
                    else:
                        warnings.warn("No running event loop during cleanup", stacklevel=2)

                if task is not None and task is not state.parent_task:
                    task.add_done_callback(cleanup_callback)
                return session
            else:
                session = _session.get()
                if session is None:
                    raise MissingSessionError
                _mark_request_session_used(session)
                return session

        def connection(self) -> _ConnectionContextManager:
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

        async def gather(self, *coros_or_futures, return_exceptions: bool = False):
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

            coros = list(coros_or_futures)

            try:
                for item in coros:
                    if asyncio.isfuture(item):
                        raise TypeError(
                            "When `max_concurrent` is set, db.gather() accepts coroutine "
                            "objects only; pre-created Task or Future inputs may already be "
                            "running outside the semaphore. Pass coroutine objects or use "
                            "db.connection()."
                        )
                    if not asyncio.iscoroutine(item):
                        raise TypeError(
                            "When `max_concurrent` is set, db.gather() accepts coroutine "
                            "objects only."
                        )
            except BaseException:
                for item in coros:
                    if asyncio.iscoroutine(item):
                        item.close()
                raise

            started = [False] * len(coros)

            async def _throttled(index, coro):
                async with _ConnectionContextManager():
                    started[index] = True
                    return await coro

            tasks = [
                asyncio.create_task(_throttled(index, coro)) for index, coro in enumerate(coros)
            ]

            try:
                return await asyncio.gather(
                    *tasks,
                    return_exceptions=return_exceptions,
                )
            except BaseException:
                for task in tasks:
                    if not task.done():
                        task.cancel()

                await asyncio.gather(*tasks, return_exceptions=True)

                for index, coro in enumerate(coros):
                    if not started[index] and asyncio.iscoroutine(coro):
                        coro.close()
                raise

    class DBSession(metaclass=DBSessionMeta):
        def __init__(
            self,
            session_args: dict | None = None,
            commit_on_exit: bool = False,
            multi_sessions: bool = False,
            max_concurrent: int | None = None,
            _request_context: bool = False,
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
            self.request_context = _request_context
            self.request_session_token = None
            self.request_session_used_token = None
            self.request_session_closed_token = None
            self._finalized = False

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
                        parent_task=parent_task,
                        commit_on_exit=self.commit_on_exit,
                        session_args=self.session_args,
                        semaphore=semaphore,
                    )
                )
            else:
                session = _Session(**self.session_args)
                self.token = _session.set(session)
                if self.request_context:
                    self.request_session_token = _request_session.set(session)
                    self.request_session_used_token = _request_session_used.set(False)
                    self.request_session_closed_token = _request_session_closed_for_streaming.set(
                        False
                    )
            return type(self)

        async def _finalize_regular_session(self, exc_type, exc_value) -> None:
            if self._finalized:
                return

            session = _request_session.get() if self.request_context else _session.get()
            if session is None:
                raise MissingSessionError

            try:
                await _finalize_session(
                    session,
                    commit_on_exit=self.commit_on_exit,
                    exc=exc_value if exc_type is not None else None,
                )
            finally:
                self._finalized = True

        async def close_request_session_for_streaming(self) -> None:
            await self._finalize_regular_session(None, None)
            if self.request_context:
                _request_session_closed_for_streaming.set(True)

        async def __aexit__(self, exc_type, exc_value, traceback):
            if self.multi_sessions:
                state = _multi_state.get()
                if state is not None:
                    state.closing = True
                _multi_sessions_ctx.reset(self.multi_sessions_token)
                cleanup_errors: list[BaseException] = []
                if state is not None:
                    # Cancel tasks parked on the semaphore first. They aren't in
                    # task_sessions yet (entry happens after acquire), so the
                    # task_sessions sweep below would miss them — they would
                    # silently take a freed slot, create a session post-closing,
                    # and race with finalisation.
                    waiting_tasks = [
                        task
                        for task in state.waiters
                        if task is not state.parent_task and not task.done()
                    ]
                    for task in waiting_tasks:
                        task.cancel()
                    if waiting_tasks:
                        await asyncio.gather(*waiting_tasks, return_exceptions=True)
                    state.waiters.clear()

                    pending_tasks = [
                        task
                        for task in state.task_sessions
                        if task is not state.parent_task and not task.done()
                    ]
                    for task in pending_tasks:
                        task.cancel()
                    if pending_tasks:
                        await asyncio.gather(*pending_tasks, return_exceptions=True)

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
                try:
                    try:
                        await self._finalize_regular_session(exc_type, exc_value)
                    except BaseException as cleanup_error:
                        if exc_type is None:
                            raise
                        warnings.warn(
                            "Suppressed session cleanup error because another exception is "
                            f"already being raised: {_cleanup_error(cleanup_error)}",
                            stacklevel=2,
                        )
                finally:
                    if self.request_context:
                        _request_session_closed_for_streaming.reset(
                            self.request_session_closed_token
                        )
                        _request_session_used.reset(self.request_session_used_token)
                        _request_session.reset(self.request_session_token)
                    _session.reset(self.token)

    # `db` is the `DBSession` class itself; its public API (`session`,
    # `connection`, `gather`, `__call__`) lives on the metaclass. Cast to the
    # `DBSessionMeta` Protocol so static type checkers see that surface
    # instead of the class object's own attributes.
    return _SQLAlchemyMiddleware, cast("_DBSessionMetaProtocol", DBSession)


SQLAlchemyMiddleware, db = create_middleware_and_session_proxy()
