# FAQ & Troubleshooting

Common errors, what they mean, and how to fix them.

## `MissingSessionError`

> No session found! Either you are not currently in a request context, or you
> need to manually create a session context…

You accessed `db.session` outside any session context. This happens in startup
hooks, scripts, background tasks, or tests.

**Fix** — wrap the access:

```python
async with db():
    result = await db.session.execute(foo.select())
```

See [Sessions & Contexts](guide/sessions.md#outside-a-request-async-with-db).

---

## `SessionNotInitialisedError`

> Session not initialised! Ensure that DBSessionMiddleware has been initialised…

You accessed `db.session` before any `SQLAlchemyMiddleware` was constructed, so
the sessionmaker doesn't exist yet.

**Fix** — make sure the middleware is added during app setup, before the access
runs:

```python
app.add_middleware(SQLAlchemyMiddleware, db_url="...")
```

In tests, construct the middleware (or run the app lifespan) before touching
`db.session`.

---

## `TimeoutError: QueuePool limit ... reached`

You launched more concurrent sessions than your pool can serve.

**Fix** — throttle with `max_concurrent` and route child sessions through
`db.gather()` or `db.connection()`:

```python
async with db(multi_sessions=True, max_concurrent=10):
    results = await db.gather(*(work(i) for i in range(1000)))
```

See [Concurrent Queries](guide/concurrency.md#throttling-with-max_concurrent).

---

## `InvalidRequestError: ... concurrent operations are not permitted`

Two coroutines used the **same** `AsyncSession` at once (e.g. `asyncio.gather`
over `db.session`). A session can only do one thing at a time.

**Fix** — give each task its own session with `multi_sessions=True`:

```python
async with db(multi_sessions=True):

    async def worker(n):
        return await db.session.execute(text(f"SELECT {n}"))

    await asyncio.gather(*(worker(i) for i in range(5)))
```

---

## `TypeError` from `db.gather()`

> When `max_concurrent` is set, db.gather() accepts coroutine objects only…

You passed a `Task` or `Future` to `db.gather()` while `max_concurrent` is set.
Those may already be running outside the semaphore.

**Fix** — pass **coroutine objects**, not tasks:

```python
# ❌ await db.gather(*[asyncio.create_task(work(i)) for i in range(10)])
# ✅
await db.gather(*(work(i) for i in range(10)))
```

Or manage your own tasks with `db.connection()` inside each.

---

## `RuntimeError: ... child tasks must access DB via db.connection() or db.gather()`

With `max_concurrent` set, a **child task** accessed `db.session` directly. That
path isn't throttled.

**Fix** — open the session through `db.connection()` (or use `db.gather()`):

```python
async with db.connection() as session:
    await session.execute(text("SELECT 1"))
```

The **parent** task may still use `db.session` directly.

---

## `RuntimeError: ... session is closed for streaming response body generation`

You reached for `db.session` from a streaming body generator. The request
session is finalized as soon as the body starts flowing, so it is no longer
there — and the same happens behind a `@app.middleware("http")`, which makes
every response look chunked.

**Fix** — own the database lifetime inside the generator, or commit before
streaming. See [Streaming Responses](guide/streaming.md).

```python
async def body():
    async with db():
        result = await db.session.stream(foo.select())
        async for row in result:
            yield serialize(row)
```

---

## `RuntimeError: This SQLAlchemy session proxy is already bound to another live engine`

You added two middlewares built from the **same** proxy with different engines,
or reused the default pair for a second database.

**Fix** — create an independent pair per database:

```python
from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

SecondMiddleware, second_db = create_middleware_and_session_proxy()
```

See [Multiple Databases](guide/multi-database.md#one-proxy-one-live-engine).

---

## Connections aren't released on shutdown

If you constructed `SQLAlchemyMiddleware(db_url=...)` outside an ASGI lifespan
(a script or ad-hoc harness), nothing triggers disposal.

**Fix** — dispose manually:

```python
middleware = SQLAlchemyMiddleware(app, db_url="...")
try:
    ...
finally:
    await middleware.dispose()
```

For a `custom_engine`, **you** own disposal: `await engine.dispose()`. See
[Engine Lifecycle](guide/engine-lifecycle.md).

---

## Graceful shutdown hangs

Engine disposal runs before the lifespan shutdown ack is forwarded, so draining
a stuck pool blocks shutdown.

**Fix** — set your ASGI server's graceful-shutdown timeout to cover the
worst-case connection close time, e.g. uvicorn:

```bash
uvicorn main:app --timeout-graceful-shutdown 30
```

---

## Does it work with SQLModel?

Yes. If `sqlmodel` is installed, the middleware uses its `AsyncSession` subclass
automatically — no configuration needed.

---

## Can I run the docs locally?

```bash
pip install -r requirements-docs.txt
mkdocs serve
```

Then open <http://127.0.0.1:8000>.
