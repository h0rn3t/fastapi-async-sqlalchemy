# Sessions & Contexts

Everything in this library revolves around one idea: **`db.session` is an
`AsyncSession` bound to the current async context.** This page explains how that
session is created, where it lives, and how to open your own contexts.

## The `db` proxy

`db` is a global object exported from the package:

```python
from fastapi_async_sqlalchemy import db
```

It exposes a small surface:

| Member             | Kind                  | Purpose                                              |
| ------------------ | --------------------- | ---------------------------------------------------- |
| `db.session`       | property              | The `AsyncSession` for the current context           |
| `db(...)`          | callable              | Open an explicit session context manager             |
| `db.connection()`  | method                | Throttled session context manager (multi-session)    |
| `db.gather(...)`   | coroutine             | Pool-aware `asyncio.gather` (multi-session)          |

`db.session` is backed by a [`ContextVar`][contextvar], so each request — each
independent async context — sees its own session. You never pass it around.

## Inside a request

When `SQLAlchemyMiddleware` is installed, every HTTP request gets a session
opened **before** your route runs. For normal responses it is finalized when the
complete body is ready, **before** the response is sent and background tasks run:

```python
@app.get("/items/{item_id}")
async def get_item(item_id: int):
    item = await db.session.get(Item, item_id)
    return item
```

The same session is visible from any function called during the request, no
arguments required:

```python
async def load_item(item_id: int) -> Item | None:
    # same session as the route — resolved from the request context
    return await db.session.get(Item, item_id)


@app.get("/items/{item_id}")
async def get_item(item_id: int):
    return await load_item(item_id)
```

### Commit on exit

By default the request session is **not** committed for you — you call
`await db.session.commit()` yourself. Set `commit_on_exit=True` to commit
automatically when the request finishes cleanly:

```python
app.add_middleware(
    SQLAlchemyMiddleware,
    db_url="postgresql+asyncpg://user:pass@localhost/app",
    commit_on_exit=True,
)
```

The finalization rules are:

- **Clean exit + `commit_on_exit=True`** → `commit()`, then `close()`.
- **Clean exit + `commit_on_exit=False`** (default) → just `close()` (uncommitted
  work is rolled back by closing).
- **Exception** → `rollback()`, then `close()`. The original exception
  propagates; a failure during rollback/commit/close is surfaced too.

!!! warning "Commit/rollback errors are not swallowed"
    If `commit()` fails, the middleware attempts a `rollback()` and raises, so a
    write failure can never be reported to the client as success.

## Outside a request: `async with db()`

Anywhere there is no request context — startup/shutdown hooks, CLI scripts,
background tasks, tests — open a session explicitly:

Starlette background tasks also need their own context: the request session is
already finalized when they start. See [HTTP Load & Background Tasks](http-load.md)
for migration details and request concurrency limits.

```python
async def get_db_fetch():
    async with db():
        result = await db.session.execute(foo.select())
        return result.fetchall()
```

`db()` accepts the same finalization options as the middleware:

```python
async with db(commit_on_exit=True):
    db.session.add(User(name="ada"))
    # committed automatically on a clean exit
```

You can also pass `session_args` to override sessionmaker arguments for that one
context:

```python
async with db(session_args={"expire_on_commit": True}):
    ...
```

### `MissingSessionError`

Accessing `db.session` with no active context raises
[`MissingSessionError`](../api-reference.md#exceptions):

```python
# ❌ no request, no `async with db()`
result = await db.session.execute(foo.select())  # MissingSessionError
```

The fix is always the same — wrap the access in a context:

```python
async with db():
    result = await db.session.execute(foo.select())
```

### `SessionNotInitialisedError`

If you access `db.session` before any `SQLAlchemyMiddleware` has been
constructed (so the sessionmaker doesn't exist yet), you get
[`SessionNotInitialisedError`](../api-reference.md#exceptions) instead. Make sure
`app.add_middleware(SQLAlchemyMiddleware, ...)` runs during app setup.

## Where to go next

- Run **many** sessions at once → [Concurrent Queries](concurrency.md)
- Stream a large response body → [Streaming Responses](streaming.md)
- Understand engine ownership and shutdown → [Engine Lifecycle](engine-lifecycle.md)

[contextvar]: https://docs.python.org/3/library/contextvars.html
