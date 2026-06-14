---
hide:
  - navigation
  - toc
---

<div class="fas-hero" markdown>

# FastAPI Async SQLAlchemy

Drop-in async SQLAlchemy middleware for FastAPI. A request-scoped
`AsyncSession` you reach through a single global `db` — no per-route
dependency wiring, no manual session plumbing.

<div class="fas-cta" markdown>
[Get started :material-arrow-right:](getting-started.md){ .md-button .md-button--primary }
[View on GitHub](https://github.com/h0rn3t/fastapi-async-sqlalchemy){ .md-button }
</div>

<div class="fas-badges" markdown>
[![PyPI](https://img.shields.io/pypi/v/fastapi_async_sqlalchemy?color=0e8a8a&label=pypi)](https://pypi.org/project/fastapi-async-sqlalchemy/)
[![Downloads](https://static.pepy.tech/badge/fastapi-async-sqlalchemy)](https://pepy.tech/project/fastapi-async-sqlalchemy)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/h0rn3t/fastapi-async-sqlalchemy/workflows/ci/badge.svg)](https://github.com/h0rn3t/fastapi-async-sqlalchemy/actions)
</div>

</div>

## Why this middleware?

SQLAlchemy's `AsyncSession` is not safe to share across concurrent tasks, and
FastAPI gives you a fresh request per coroutine. This middleware binds **one
session to each request context** using a Python [`ContextVar`][contextvar], so
`db.session` always resolves to the right session for the request you're in —
whether you access it from a route, a service function, or a background helper.

```python
from fastapi import FastAPI
from fastapi_async_sqlalchemy import SQLAlchemyMiddleware, db
from sqlalchemy import text

app = FastAPI()
app.add_middleware(
    SQLAlchemyMiddleware,
    db_url="postgresql+asyncpg://user:pass@localhost:5432/app",
)

@app.get("/ping")
async def ping():
    result = await db.session.execute(text("SELECT 1"))
    return {"db": result.scalar()}
```

No `Depends(get_session)`, no passing the session down every call. Access the
session anywhere in the request with `db.session`.

## Features

<div class="grid cards" markdown>

-   :material-database-sync:{ .lg .middle } __Request-scoped sessions__

    ---

    A `ContextVar`-backed `AsyncSession` per request. Reach it from anywhere
    with `db.session` — no dependency injection boilerplate.

    [:octicons-arrow-right-24: Sessions & contexts](guide/sessions.md)

-   :material-engine:{ .lg .middle } __Engine lifecycle done right__

    ---

    Pass a `db_url` and the middleware owns and disposes the engine on
    shutdown; pass a `custom_engine` and you keep ownership.

    [:octicons-arrow-right-24: Engine lifecycle](guide/engine-lifecycle.md)

-   :material-arrow-decision:{ .lg .middle } __Pool-throttled concurrency__

    ---

    Run many queries in parallel without exhausting the pool. `db.gather()`
    and `db.connection()` cap in-flight sessions at `max_concurrent`.

    [:octicons-arrow-right-24: Concurrent queries](guide/concurrency.md)

-   :material-transit-connection-variant:{ .lg .middle } __Multiple databases__

    ---

    Build independent middleware/proxy pairs with
    `create_middleware_and_session_proxy()` — one per database.

    [:octicons-arrow-right-24: Multiple databases](guide/multi-database.md)

-   :material-download-network:{ .lg .middle } __Streaming-aware__

    ---

    Clear rules for `StreamingResponse` so the session lifetime belongs to the
    body, not a closed request transaction.

    [:octicons-arrow-right-24: Streaming responses](guide/streaming.md)

-   :material-language-python:{ .lg .middle } __Typed & SQLModel-ready__

    ---

    Ships `py.typed`, a `DBSessionMeta` Protocol for full autocomplete, and
    works transparently with `sqlmodel`.

    [:octicons-arrow-right-24: Type hints](guide/type-hints.md)

</div>

## Installation

```bash
pip install fastapi-async-sqlalchemy
```

Requires Python 3.12+, `starlette>=0.40`, and `SQLAlchemy>=2.0`. Add the async
driver for your database (`asyncpg`, `aiomysql`, `aiosqlite`, …).

[contextvar]: https://docs.python.org/3/library/contextvars.html
