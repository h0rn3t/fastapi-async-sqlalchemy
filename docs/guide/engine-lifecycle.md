# Engine Lifecycle

The middleware can either **own** the SQLAlchemy async engine or **borrow** one
you created. Ownership decides who disposes the connection pool — and getting
this wrong leaks connections on shutdown. This page makes the rules explicit.

## Two ways to provide an engine

=== "Middleware owns the engine (`db_url`)"

    ```python
    app.add_middleware(
        SQLAlchemyMiddleware,
        db_url="postgresql+asyncpg://user:pass@localhost/app",
        engine_args={"pool_size": 5, "max_overflow": 10},
    )
    ```

    The middleware calls `create_async_engine(db_url, **engine_args)`, **owns**
    the result, and disposes it during ASGI lifespan shutdown.

=== "You own the engine (`custom_engine`)"

    ```python
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("postgresql+asyncpg://user:pass@localhost/app")
    app.add_middleware(SQLAlchemyMiddleware, custom_engine=engine)

    # later, in your own shutdown / test cleanup:
    await engine.dispose()
    ```

    The middleware uses your engine but **never disposes it**. Disposal is your
    responsibility.

!!! note "One of the two is required"
    You must pass either `db_url` or `custom_engine`. Passing neither raises
    `ValueError`.

## Disposal during ASGI lifespan

For a middleware-owned engine (`db_url`), disposal is automatic. It happens when
the lifespan ends — including failure paths — so a raising shutdown handler
can't leak the pool:

- `lifespan.shutdown.complete`
- `lifespan.shutdown.failed`
- `lifespan.startup.failed`

The engine is disposed **once**, for the application lifetime — not per request.

```python
from fastapi.testclient import TestClient

# Running the lifespan (e.g. via TestClient context) triggers disposal:
with TestClient(app):
    ...
# <- engine disposed here
```

!!! warning "Disposal blocks the shutdown ack"
    Engine disposal runs **before** the lifespan acknowledgement is forwarded to
    the ASGI server, so a slow pool drain delays graceful shutdown. Configure
    your server's graceful-shutdown timeout (e.g. uvicorn's
    `--timeout-graceful-shutdown`) to cover the worst-case time to close active
    connections.

## Manual disposal outside a lifespan

When you build `SQLAlchemyMiddleware(db_url=...)` **outside** an ASGI lifespan —
a script, an ad-hoc harness, a non-ASGI runtime — there is no
`lifespan.shutdown` event, so nothing triggers disposal. Call `dispose()`
yourself:

```python
middleware = SQLAlchemyMiddleware(app, db_url="postgresql+asyncpg://...")
try:
    ...  # use db.session
finally:
    await middleware.dispose()
```

`dispose()` is:

- **Idempotent on success** — calling it again is a no-op.
- **Safe to retry on failure** — the proxy's session bindings are cleared
  deterministically, so a later call actually re-attempts `engine.dispose()`
  rather than silently no-op'ing on a half-disposed engine.
- **A no-op for borrowed engines** — if you passed `custom_engine`, `dispose()`
  does nothing; you own that engine.

The same guidance applies to each pair returned by
[`create_middleware_and_session_proxy()`](multi-database.md).

## Summary

| You pass         | Engine created by | Disposed by                     | When                                   |
| ---------------- | ----------------- | ------------------------------- | -------------------------------------- |
| `db_url`         | the middleware    | the middleware                  | lifespan shutdown, or `dispose()`      |
| `custom_engine`  | you               | you (`await engine.dispose()`)  | whenever your own cleanup runs         |
