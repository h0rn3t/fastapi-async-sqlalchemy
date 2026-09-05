# Getting Started

This page takes you from an empty project to a running FastAPI app backed by an
async SQLAlchemy session in a few minutes.

## Requirements

- **Python** 3.12 or newer
- **starlette** ≥ 0.40 and **SQLAlchemy** ≥ 2.0 (installed automatically)
- An **async database driver** for your engine

| Database   | Driver       | Example URL                                             |
| ---------- | ------------ | ------------------------------------------------------- |
| PostgreSQL | `asyncpg`    | `postgresql+asyncpg://user:pass@localhost:5432/app`     |
| MySQL      | `aiomysql`   | `mysql+aiomysql://user:pass@localhost:3306/app`         |
| SQLite     | `aiosqlite`  | `sqlite+aiosqlite:///./app.db`                          |

## Install

```bash
pip install fastapi-async-sqlalchemy

# plus a driver, e.g. PostgreSQL
pip install asyncpg
```

It also works out of the box with [`sqlmodel`](https://sqlmodel.tiangolo.com/) —
if `sqlmodel` is installed, its `AsyncSession` subclass is used automatically.

## Your first app

```python title="main.py"
from fastapi import FastAPI
from fastapi_async_sqlalchemy import SQLAlchemyMiddleware, db
from sqlalchemy import column, table

app = FastAPI()

app.add_middleware(
    SQLAlchemyMiddleware,
    db_url="sqlite+aiosqlite:///./app.db",
)

# a lightweight table reference for the example
files = table("ms_files", column("id"))


@app.get("/files")
async def list_files():
    result = await db.session.execute(files.select())
    return result.fetchall()
```

Run it:

```bash
uvicorn main:app --reload
```

Open <http://127.0.0.1:8000/files>. The middleware opened a session for the
request, made it available as `db.session`, and closed it when the response was
sent.

!!! tip "That's the whole idea"
    You never created or passed a session. `db.session` is bound to the current
    request via a `ContextVar`, so it resolves correctly even from helper
    functions called deep inside the request.

## Configuring the engine

Pass engine and session options through `engine_args` / `session_args`. These
are forwarded to SQLAlchemy's
[`create_async_engine`](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
and `async_sessionmaker`. `session_args` may also override the defaults the
middleware sets itself — `expire_on_commit=False` and `class_`.

```python
app.add_middleware(
    SQLAlchemyMiddleware,
    db_url="postgresql+asyncpg://user:pass@localhost:5432/app",
    engine_args={
        "echo": True,  # log every SQL statement
        "pool_pre_ping": True,  # validate connections before use
        "pool_size": 5,  # connections kept open
        "max_overflow": 10,  # extra connections allowed above pool_size
    },
    commit_on_exit=True,  # commit the request session on a clean exit
)
```

## Using the session outside a request

Outside the request/response cycle (startup hooks, scripts, workers) there is no
middleware to open a session for you. Open one explicitly with the `db` context
manager:

```python
async def warm_cache():
    async with db():
        result = await db.session.execute(files.select())
        return result.fetchall()


@app.on_event("startup")
async def on_startup():
    await warm_cache()
```

## Next steps

<div class="grid cards" markdown>

- :material-database-sync: [__Sessions & contexts__](guide/sessions.md) — how `db.session` and `async with db()` behave
- :material-engine: [__Engine lifecycle__](guide/engine-lifecycle.md) — ownership, disposal, graceful shutdown
- :material-arrow-decision: [__Concurrent queries__](guide/concurrency.md) — run parallel queries safely
- :material-book-open-variant: [__API reference__](api-reference.md) — every public symbol

</div>
