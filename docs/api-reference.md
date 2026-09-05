# API Reference

Everything the package exports from `fastapi_async_sqlalchemy`.

```python
from fastapi_async_sqlalchemy import (
    SQLAlchemyMiddleware,
    db,
    create_middleware_and_session_proxy,
    DBSessionMeta,
)
```

---

## `SQLAlchemyMiddleware`

ASGI middleware that opens a request-scoped `AsyncSession` and finalizes it when
the response is sent. Add it with `app.add_middleware(...)`.

```python
app.add_middleware(
    SQLAlchemyMiddleware,
    db_url=None,
    custom_engine=None,
    engine_args=None,
    session_args=None,
    commit_on_exit=False,
    exclude_paths=None,
    pool_warn_threshold=None,
    pool_warn_interval=10.0,
    max_concurrent_requests=None,
    request_queue_timeout=0.1,
)
```

**Parameters**

| Name                  | Type                     | Default | Description                                                                                 |
| --------------------- | ------------------------ | ------- | ------------------------------------------------------------------------------------------- |
| `db_url`              | `str \| URL \| None`     | `None`  | Database URL. The middleware creates and **owns** the engine. Mutually exclusive with `custom_engine`. |
| `custom_engine`       | `AsyncEngine \| None`    | `None`  | A pre-built engine you **own**. The middleware uses it but never disposes it.                |
| `engine_args`         | `dict \| None`           | `None`  | Forwarded to `create_async_engine` (only when `db_url` is used). E.g. `pool_size`, `echo`.  |
| `session_args`        | `dict \| None`           | `None`  | Forwarded to `async_sessionmaker`.                                                           |
| `commit_on_exit`      | `bool`                   | `False` | Commit the request session on a clean exit. See [Sessions](guide/sessions.md#commit-on-exit). |
| `exclude_paths`       | `Iterable[str] \| None`  | `None`  | Paths that get **no** request session (exact match on the ASGI `scope["path"]`). `db.session` there raises `MissingSessionError`. See [Health Checks](guide/health-checks.md#4-keep-sessionless-paths-sessionless). |
| `pool_warn_threshold` | `float \| None`          | `None`  | Log a `WARNING` once the pool is this share of its capacity, e.g. `0.9`. Must be within `(0, 1]`, else `ValueError`. |
| `pool_warn_interval`  | `float`                  | `10.0`  | Minimum seconds between those warnings.                                                      |
| `max_concurrent_requests` | `int \| None` | `None` | Optional shared HTTP request limit per middleware instance. Positive integer. Excluded paths bypass it. |
| `request_queue_timeout` | `float` | `0.1` | Finite positive admission wait in seconds. Expiry returns 503 with `Retry-After: 1` before the route runs. |

See [HTTP Load & Background Tasks](guide/http-load.md) for sizing, streaming
behavior and the requirement for background tasks to open their own sessions.

!!! warning "Exactly one engine source"
    Passing neither `db_url` nor `custom_engine` raises `ValueError`. Binding a
    proxy that is already bound to a different live engine raises `RuntimeError`
    — use [`create_middleware_and_session_proxy()`](#create_middleware_and_session_proxy)
    for additional databases.

### `await middleware.dispose()`

Dispose the middleware-owned engine. No-op for a `custom_engine`. Idempotent on
success and safe to retry on failure. Call it manually when running outside an
ASGI lifespan. See [Engine Lifecycle](guide/engine-lifecycle.md#manual-disposal-outside-a-lifespan).

---

## `db`

The global session proxy. Its public API lives on its metaclass; annotate it
with [`DBSessionMeta`](#dbsessionmeta-dbsessiontype).

### `db.session`

: **Property** → `AsyncSession`

    The session bound to the current async context.

    - In `multi_sessions` mode, returns the calling task's own session.
    - Raises [`SessionNotInitialisedError`](#exceptions) if no middleware has
      been constructed yet.
    - Raises [`MissingSessionError`](#exceptions) if there is no active request
      or `async with db()` context.
    - Raises `RuntimeError` if it would have to create a session under
      `db(multi_sessions=True, pool_timeout=...)` — see
      [the warning below](#db).

### `db(...)`

: **Call** → async context manager

    Open an explicit session context.

    ```python
    db(
        session_args=None,
        commit_on_exit=False,
        multi_sessions=False,
        max_concurrent=None,
        pool_timeout=None,
    )
    ```

    | Name             | Type                | Default | Description                                                       |
    | ---------------- | ------------------- | ------- | ----------------------------------------------------------------- |
    | `session_args`   | `dict \| None`      | `None`  | Per-context overrides for the sessionmaker.                       |
    | `commit_on_exit` | `bool`              | `False` | Commit on a clean exit of the block.                              |
    | `multi_sessions` | `bool`              | `False` | Give each task its own session. See [Concurrency](guide/concurrency.md). |
    | `max_concurrent` | `int \| None`       | `None`  | Cap simultaneous sessions. Must be `>= 1`, else `ValueError`.     |
    | `pool_timeout`   | `float \| None`     | `None`  | Seconds to wait for a pool connection before raising [`PoolTimeoutError`](#pooltimeouterror). Must be `> 0`, else `ValueError`. |

    ```python
    async with db(commit_on_exit=True):
        db.session.add(obj)
    ```

    `pool_timeout` overrides the engine-wide `pool_timeout` for this block only,
    and forces the connection to be checked out on entry rather than on first
    query. See [Health Checks](guide/health-checks.md#2-fail-fast-instead-of-queueing).

    ```python
    async with db(pool_timeout=1):  # fail after 1s, not after 60
        await db.session.execute(text("SELECT 1"))
    ```

    !!! warning "`pool_timeout` with `multi_sessions=True`"
        In multi-session mode, `db.session` creates a session per task. It is a
        synchronous property, so it cannot await a checkout and cannot apply the
        deadline — the first query would park on the engine-wide `pool_timeout`
        instead. Combining the two therefore raises `RuntimeError` on a
        `db.session` that would create a new session; use
        [`db.connection()`](#dbconnectiontimeoutnone) or
        [`db.gather()`](#await-dbgathercoros-return_exceptionsfalse), which check the
        connection out within the deadline. Reading `db.session` inside a
        `db.connection()` block or a `db.gather()` coroutine is fine — it
        returns the session that block already checked out.

        ```python
        async with db(multi_sessions=True, pool_timeout=1):
            async with db.connection() as session:
                await session.execute(text("SELECT 1"))
        ```

### `db.connection(timeout=None)`

: **Method** → async context manager yielding `AsyncSession`

    Throttled session access for `multi_sessions` mode. Waits for a semaphore
    slot (when `max_concurrent` is set) before creating a session, then releases
    it on exit. Without `max_concurrent` it simply creates and cleans up a
    session.

    ```python
    async with db(multi_sessions=True, max_concurrent=10):
        async with db.connection() as session:
            await session.execute(text("SELECT 1"))
    ```

    `timeout` caps how long entering the block may wait for a pool connection,
    raising [`PoolTimeoutError`](#pooltimeouterror) on expiry. It defaults to
    the enclosing context's `pool_timeout`.

    ```python
    async with db.connection(timeout=0.5) as session:
        await session.execute(text("SELECT 1"))
    ```

### `db.pool_status()`

: **Method** → `dict`

    A live snapshot of the engine's connection pool — the thing to export as
    metrics and alert on before saturation becomes a checkout timeout.

    ```python
    {
        "pool_class": "AsyncAdaptedQueuePool",
        "size": 20,
        "max_overflow": 50,
        "capacity": 70,
        "checked_in": 3,
        "checked_out": 67,
        "available": 3,
        "saturation": 0.957,
    }
    ```

    Pools that don't track connections (`NullPool`, `StaticPool`) report `None`
    for every numeric field. Raises [`SessionNotInitialisedError`](#exceptions)
    when no middleware has been constructed.

    Unlimited queue pools (`max_overflow=-1` or `pool_size=0`) report `None` for
    `capacity`, `available` and `saturation`; their connection counters remain available.

### `await db.gather(*coros, return_exceptions=False)`

: **Coroutine** → `list`

    Pool-aware drop-in for `asyncio.gather`. Each coroutine acquires a slot (and
    a session) before running and releases it afterwards, so no more than
    `max_concurrent` connections are in use at once.

    A context `pool_timeout` is applied the same way, so each coroutine fails
    fast with [`PoolTimeoutError`](#pooltimeouterror) instead of parking on the
    engine-wide deadline.

    - Accepts **coroutine objects only** when `max_concurrent` or `pool_timeout`
      is set; pre-created `Task`/`Future` inputs raise `TypeError` because they
      may already be running outside the managed session.
    - With neither set, delegates directly to `asyncio.gather`.

    ```python
    async with db(multi_sessions=True, max_concurrent=10):
        results = await db.gather(*(work(i) for i in range(100)))
    ```

---

## `create_middleware_and_session_proxy()`

```python
create_middleware_and_session_proxy() -> tuple[type, DBSessionMeta]
```

Build a fresh, fully isolated `(middleware_class, db_proxy)` pair with its own
`ContextVar` state and engine binding. Use one pair per independent app or
database.

```python
FirstMiddleware, first_db = create_middleware_and_session_proxy()
SecondMiddleware, second_db = create_middleware_and_session_proxy()
```

The package's default exports are created exactly this way:

```python
SQLAlchemyMiddleware, db = create_middleware_and_session_proxy()
```

See [Multiple Databases](guide/multi-database.md).

---

## `DBSessionMeta`

A structural `Protocol` (at type-check time) and the runtime metaclass of `db`
(at runtime). Use as an annotation for the `db` proxy.
See [Type Hints](guide/type-hints.md).

```python
def get_db() -> DBSessionMeta:
    return db
```

---

## Exceptions

All three live in `fastapi_async_sqlalchemy` (and `fastapi_async_sqlalchemy.exceptions`).

### `MissingSessionError`

Raised when `db.session` is accessed with **no active session context** — you're
not in a request and haven't opened `async with db()`.

```python
async with db():
    await db.session.execute(foo.select())  # ✅ fix
```

### `SessionNotInitialisedError`

Raised when `db.session` is accessed before any `SQLAlchemyMiddleware` has been
**constructed**, so the sessionmaker doesn't exist yet. Ensure the middleware is
added during app setup.

### `PoolTimeoutError`

Raised when a connection could not be checked out within the deadline set by
`db(pool_timeout=...)` or `db.connection(timeout=...)`. Subclasses the builtin
`TimeoutError`, so existing `except TimeoutError` handlers still catch it.

| Attribute     | Type            | Description                                                      |
| ------------- | --------------- | ---------------------------------------------------------------- |
| `timeout`     | `float \| None` | The deadline, in seconds, that was exceeded.                     |
| `retry_after` | `int`           | Suggested `Retry-After` value, in whole seconds.                 |
| `pool_status` | `dict \| None`  | Pool snapshot at failure time, same shape as `db.pool_status()`. |

Map it to a controlled `503` so callers back off instead of receiving a `500`:

```python
@app.exception_handler(PoolTimeoutError)
async def pool_exhausted(request, exc: PoolTimeoutError):
    return JSONResponse(
        {"detail": "database connection pool exhausted"},
        status_code=503,
        headers={"Retry-After": str(exc.retry_after)},
    )
```

See [Health Checks & Pool Saturation](guide/health-checks.md).

---

## Version

The installed version is available as:

```python
import fastapi_async_sqlalchemy

fastapi_async_sqlalchemy.__version__
```
