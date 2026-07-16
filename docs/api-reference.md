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
)
```

**Parameters**

| Name             | Type                     | Default | Description                                                                                 |
| ---------------- | ------------------------ | ------- | ------------------------------------------------------------------------------------------- |
| `db_url`         | `str \| URL \| None`     | `None`  | Database URL. The middleware creates and **owns** the engine. Mutually exclusive with `custom_engine`. |
| `custom_engine`  | `AsyncEngine \| None`    | `None`  | A pre-built engine you **own**. The middleware uses it but never disposes it.                |
| `engine_args`    | `dict \| None`           | `None`  | Forwarded to `create_async_engine` (only when `db_url` is used). E.g. `pool_size`, `echo`.  |
| `session_args`   | `dict \| None`           | `None`  | Forwarded to `async_sessionmaker`.                                                           |
| `commit_on_exit` | `bool`                   | `False` | Commit the request session on a clean exit. See [Sessions](guide/sessions.md#commit-on-exit). |

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

### `db(...)`

: **Call** → async context manager

    Open an explicit session context.

    ```python
    db(
        session_args=None,
        commit_on_exit=False,
        multi_sessions=False,
        max_concurrent=None,
    )
    ```

    | Name             | Type                | Default | Description                                                       |
    | ---------------- | ------------------- | ------- | ----------------------------------------------------------------- |
    | `session_args`   | `dict \| None`      | `None`  | Per-context overrides for the sessionmaker.                       |
    | `commit_on_exit` | `bool`              | `False` | Commit on a clean exit of the block.                              |
    | `multi_sessions` | `bool`              | `False` | Give each task its own session. See [Concurrency](guide/concurrency.md). |
    | `max_concurrent` | `int \| None`       | `None`  | Cap simultaneous sessions. Must be `>= 1`, else `ValueError`.     |

    ```python
    async with db(commit_on_exit=True):
        db.session.add(obj)
    ```

### `db.connection()`

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

### `await db.gather(*coros, return_exceptions=False)`

: **Coroutine** → `list`

    Pool-aware drop-in for `asyncio.gather`. Each coroutine acquires a slot (and
    a session) before running and releases it afterwards, so no more than
    `max_concurrent` connections are in use at once.

    - Accepts **coroutine objects only** when `max_concurrent` is set;
      pre-created `Task`/`Future` inputs raise `TypeError`.
    - With no `max_concurrent`, delegates directly to `asyncio.gather`.

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

Both live in `fastapi_async_sqlalchemy` (and `fastapi_async_sqlalchemy.exceptions`).

### `MissingSessionError`

Raised when `db.session` is accessed with **no active session context** — you're
not in a request and haven't opened `async with db()`.

```python
async with db():
    await db.session.execute(foo.select())   # ✅ fix
```

### `SessionNotInitialisedError`

Raised when `db.session` is accessed before any `SQLAlchemyMiddleware` has been
**constructed**, so the sessionmaker doesn't exist yet. Ensure the middleware is
added during app setup.

---

## Version

The installed version is available as:

```python
import fastapi_async_sqlalchemy
fastapi_async_sqlalchemy.__version__
```
