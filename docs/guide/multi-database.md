# Multiple Databases

The default `SQLAlchemyMiddleware` / `db` pair is bound to **one** engine. To
talk to several independent databases, create a separate middleware/session
proxy pair for each with `create_middleware_and_session_proxy()`.

## Create one pair per database

```python title="databases.py"
from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

FirstSQLAlchemyMiddleware, first_db = create_middleware_and_session_proxy()
SecondSQLAlchemyMiddleware, second_db = create_middleware_and_session_proxy()
```

Each call returns an independent `(middleware_class, db_proxy)` tuple with its
own `ContextVar` state and engine binding.

!!! info "The default pair is just a pre-made instance"
    `SQLAlchemyMiddleware, db = create_middleware_and_session_proxy()` is exactly
    how the package builds the defaults it exports. Calling it yourself gives you
    additional, fully isolated pairs.

## Wire them into the app

```python title="main.py"
from fastapi import FastAPI

from databases import FirstSQLAlchemyMiddleware, SecondSQLAlchemyMiddleware
from routes import router

app = FastAPI()
app.include_router(router)

app.add_middleware(
    FirstSQLAlchemyMiddleware,
    db_url="postgresql+asyncpg://user:pass@localhost:5432/primary_db",
    engine_args={"pool_size": 5, "max_overflow": 10},
)
app.add_middleware(
    SecondSQLAlchemyMiddleware,
    db_url="mysql+aiomysql://user:pass@localhost:3306/secondary_db",
    engine_args={"pool_size": 5, "max_overflow": 10},
)
```

## Use the right proxy in each route

```python title="routes.py"
from fastapi import APIRouter
from sqlalchemy import column, table

from databases import first_db, second_db

router = APIRouter()
files = table("ms_files", column("id"))


@router.get("/first-db-files")
async def get_files_from_first_db():
    result = await first_db.session.execute(files.select())
    return result.fetchall()


@router.get("/second-db-files")
async def get_files_from_second_db():
    result = await second_db.session.execute(files.select())
    return result.fetchall()
```

Each proxy resolves its own request-scoped session, so `first_db.session` and
`second_db.session` never collide.

## One proxy, one live engine

A proxy is **bound to a single live engine**. Reusing the same proxy with a
different live engine is rejected:

```text
RuntimeError: This SQLAlchemy session proxy is already bound to another live
engine. Use create_middleware_and_session_proxy() for independent apps or
databases.
```

This guard exists so requests can never silently switch to a different database
binding. The fix is to use a **fresh pair** per app or database — exactly what
`create_middleware_and_session_proxy()` is for.

!!! tip "Rebinding after disposal"
    The binding is cleared when the owning middleware's engine is disposed (via
    lifespan shutdown or `await middleware.dispose()`). After disposal the proxy
    is free to bind a new engine — useful in test suites that build and tear down
    an app per test.

## Concurrency still applies per proxy

Each proxy supports the full [concurrency API](concurrency.md) independently:

```python
async with first_db(multi_sessions=True, max_concurrent=10):
    results = await first_db.gather(*(work(i) for i in range(100)))
```
