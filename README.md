# SQLAlchemy FastAPI middleware

[![ci](https://img.shields.io/badge/Support-Ukraine-FFD500?style=flat&labelColor=005BBB)](https://img.shields.io/badge/Support-Ukraine-FFD500?style=flat&labelColor=005BBB)
[![ci](https://github.com/h0rn3t/fastapi-async-sqlalchemy/workflows/ci/badge.svg)](https://github.com/h0rn3t/fastapi-async-sqlalchemy/workflows/ci/badge.svg)
[![codecov](https://codecov.io/gh/h0rn3t/fastapi-async-sqlalchemy/branch/main/graph/badge.svg?token=F4NJ34WKPY)](https://codecov.io/gh/h0rn3t/fastapi-async-sqlalchemy)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![pip](https://img.shields.io/pypi/v/fastapi_async_sqlalchemy?color=blue)](https://pypi.org/project/fastapi-async-sqlalchemy/)
[![Downloads](https://static.pepy.tech/badge/fastapi-async-sqlalchemy)](https://pepy.tech/project/fastapi-async-sqlalchemy)
[![Updates](https://pyup.io/repos/github/h0rn3t/fastapi-async-sqlalchemy/shield.svg)](https://pyup.io/repos/github/h0rn3t/fastapi-async-sqlalchemy/)
[![Docs](https://img.shields.io/badge/docs-mkdocs--material-0e8a8a)](https://h0rn3t.github.io/fastapi-async-sqlalchemy/)

### Description

Provides SQLAlchemy middleware for FastAPI using AsyncSession and async engine.

📖 **Full documentation: <https://h0rn3t.github.io/fastapi-async-sqlalchemy/>**

### Install

```bash
      pip install fastapi-async-sqlalchemy
```


It also works with ```sqlmodel```


### Examples

Note that the session object provided by ``db.session`` is based on the Python3.7+ ``ContextVar``. This means that
each session is linked to the individual request context in which it was created.

```python

from fastapi import FastAPI
from fastapi_async_sqlalchemy import SQLAlchemyMiddleware
from fastapi_async_sqlalchemy import db  # provide access to a database session
from sqlalchemy import column
from sqlalchemy import table

app = FastAPI()
app.add_middleware(
    SQLAlchemyMiddleware,
    db_url="postgresql+asyncpg://user:user@192.168.88.200:5432/primary_db",
    engine_args={              # engine arguments example
        "echo": True,          # print all SQL statements
        "pool_pre_ping": True, # feature will normally emit SQL equivalent to “SELECT 1” each time a connection is checked out from the pool
        "pool_size": 5,        # number of connections to keep open at a time
        "max_overflow": 10,    # number of connections to allow to be opened above pool_size
    },
)
# Engines created from ``db_url`` are owned by the middleware and are disposed
# during the application shutdown lifespan. Tests that need shutdown behavior
# should run the app lifespan, for example with ``with TestClient(app)``.
# once the middleware is applied, any route can then access the database session
# from the global ``db``

foo = table("ms_files", column("id"))

# Usage inside of a route
@app.get("/")
async def get_files():
    result = await db.session.execute(foo.select())
    return result.fetchall()

async def get_db_fetch():
    # It uses the same ``db`` object and use it as a context manager:
    async with db():
        result = await db.session.execute(foo.select())
        return result.fetchall()

# Usage inside of a route using a db context
@app.get("/db_context")
async def db_context():
    return await get_db_fetch()

# Usage outside of a route using a db context
@app.on_event("startup")
async def on_startup():
    # We are outside of a request context, therefore we cannot rely on ``SQLAlchemyMiddleware``
    # to create a database session for us.
    result = await get_db_fetch()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)

```

#### Engine ownership

When the middleware receives ``db_url``, it creates and owns the async engine.
The engine is kept for the application lifetime and disposed when the ASGI
lifespan shutdown completes. It is not disposed per request. Disposal also
runs when the lifespan ends with a failure (``lifespan.shutdown.failed`` or
``lifespan.startup.failed``), so a raising user shutdown handler does not leak
the connection pool.

Engine disposal happens before the lifespan acknowledgement is forwarded to
the ASGI server, so a stuck pool drain will block the server's graceful
shutdown ack. Configure your ASGI server's graceful shutdown timeout (for
example uvicorn's ``--timeout-graceful-shutdown``) so it accommodates the
worst-case time required to close active connections.

When the middleware receives ``custom_engine``, the caller owns that engine. The
middleware will use it but will not dispose it during application shutdown:

```python
from sqlalchemy.ext.asyncio import create_async_engine

engine = create_async_engine("postgresql+asyncpg://user:pass@host/db")
app.add_middleware(SQLAlchemyMiddleware, custom_engine=engine)

# Later, in caller-managed shutdown code or test cleanup:
await engine.dispose()
```

#### Manual disposal outside ASGI lifespan

When ``SQLAlchemyMiddleware(db_url=...)`` is constructed outside an ASGI
application lifespan — for example in a script, an ad-hoc test harness, or
when embedding the middleware in a non-ASGI runtime — there is no
``lifespan.shutdown`` event to trigger engine disposal. In that case call
``await middleware.dispose()`` explicitly so the middleware-owned engine is
released:

```python
middleware = SQLAlchemyMiddleware(app, db_url="postgresql+asyncpg://...")
try:
    ...  # use db.session
finally:
    await middleware.dispose()
```

``dispose()`` is idempotent on success and is safe to retry if it raises:
the proxy session bindings are cleared deterministically so a subsequent
call actually re-attempts the underlying ``engine.dispose()``. The same
guidance applies to each pair created by
``create_middleware_and_session_proxy()``.

#### Request transactions and streaming responses

When ``SQLAlchemyMiddleware(..., commit_on_exit=True)`` manages a normal
non-streaming HTTP request, the request session is committed before
``http.response.start`` is forwarded to the ASGI server. If commit, rollback,
or close fails, the failure happens before a successful response is reported to
the client.

Streaming response body generation has a different lifetime from a normal
request transaction. Do not rely on the middleware-managed request session to
stay open while a ``StreamingResponse``/``FileResponse`` yields chunks. Open an
explicit session inside the generator so the body owns the database lifetime:

```python
from fastapi.responses import StreamingResponse

@app.get("/export")
async def export():
    async def rows():
        async with db():
            result = await db.session.stream(foo.select())
            async for row in result:
                yield f"{row.id}\n".encode()
    return StreamingResponse(rows(), media_type="text/plain")
```

Implicit ``commit_on_exit=True`` is not a safe way to report streaming write
success: the response may have already started before an unbounded body is
finished. If a streaming route needs database writes, either complete and
commit the write in a separate explicit ``async with db(commit_on_exit=True)``
block before creating the streaming response, or make the streaming generator
use an explicit ``async with db(commit_on_exit=True)`` block and design the API
so clients do not treat early chunks as write success.

For applications that previously used ``db.session`` directly inside streaming
generators, move that code into an explicit generator-owned context as shown
above. This keeps database access available for the whole body while making it
clear that the session lifetime belongs to the stream, not the original request
transaction.

#### Type hints for `db`

Use `DBSessionMeta` (or its alias `DBSessionType`) when you need to annotate
a function or attribute that holds the `db` proxy:

```python
from fastapi_async_sqlalchemy import DBSessionMeta, db

def get_db() -> DBSessionMeta:
    return db
```

At runtime `DBSessionMeta` is the metaclass of `db`, so `isinstance(db,
DBSessionMeta)` and `type(db) is DBSessionMeta` both work. For static type
checkers (mypy, pyright) and IDE autocompletion `DBSessionMeta` resolves to
a structural `Protocol` describing the public API (`session`, `connection`,
`gather`, and the `db(...)` call).

#### SQLAlchemy events (`before_insert`, `after_insert`, ...)

SQLAlchemy's event system is independent of the session/engine — register
listeners on your mapped classes (or on `Mapper`/`Session`) with
`sqlalchemy.event.listens_for` exactly as you would with a synchronous
SQLAlchemy setup. The middleware does not change how events fire.

```python
from datetime import datetime
from sqlalchemy import Column, DateTime, Integer, String, event
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


@event.listens_for(User, "before_insert")
def normalize(mapper, connection, target):
    target.username = target.username.lower().strip()


@event.listens_for(User, "before_update")
def touch_updated_at(mapper, connection, target):
    target.updated_at = datetime.utcnow()


@event.listens_for(User, "after_insert")
def log_insert(mapper, connection, target):
    print(f"user created: id={target.id}")
```

Mapper-level events (`before_insert`, `after_insert`, `before_update`,
`after_update`, `before_delete`, `after_delete`) receive a synchronous
`connection` argument — do **not** `await` inside them and do **not** call
async ORM APIs there. If you need async work after a write, do it after
`await db.session.commit()` returns, or use `Session`-level events such as
`after_flush` / `after_commit` and schedule async work from there.

A complete runnable example with validation, timestamps, logging, and
soft-delete hooks lives at [examples/events_example.py](examples/events_example.py).

#### Usage of multiple databases

databases.py

```python
from fastapi import FastAPI
from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

FirstSQLAlchemyMiddleware, first_db = create_middleware_and_session_proxy()
SecondSQLAlchemyMiddleware, second_db = create_middleware_and_session_proxy()
```

Use a separate middleware/session proxy pair for each independent app or
database. Reusing the same proxy with a different live engine is rejected so
requests cannot silently switch to another database binding.

main.py

```python
from fastapi import FastAPI

from databases import FirstSQLAlchemyMiddleware, SecondSQLAlchemyMiddleware
from routes import router

app = FastAPI()

app.include_router(router)

app.add_middleware(
    FirstSQLAlchemyMiddleware,
    db_url="postgresql+asyncpg://user:user@192.168.88.200:5432/primary_db",
    engine_args={
        "pool_size": 5,
        "max_overflow": 10,
    },
)
app.add_middleware(
    SecondSQLAlchemyMiddleware,
    db_url="mysql+aiomysql://user:user@192.168.88.200:5432/primary_db",
    engine_args={
        "pool_size": 5,
        "max_overflow": 10,
    },
)
```

routes.py

```python
import asyncio

from fastapi import APIRouter
from sqlalchemy import column, table, text

from databases import first_db, second_db

router = APIRouter()

foo = table("ms_files", column("id"))

@router.get("/first-db-files")
async def get_files_from_first_db():
    result = await first_db.session.execute(foo.select())
    return result.fetchall()


@router.get("/second-db-files")
async def get_files_from_second_db():
    result = await second_db.session.execute(foo.select())
    return result.fetchall()


@router.get("/concurrent-queries")
async def parallel_select():
    async with first_db(multi_sessions=True, max_concurrent=10):
        async def execute_query(query):
            async with first_db.connection() as session:
                return await session.execute(text(query))

        tasks = [
            asyncio.create_task(execute_query("SELECT 1")),
            asyncio.create_task(execute_query("SELECT 2")),
            asyncio.create_task(execute_query("SELECT 3")),
            asyncio.create_task(execute_query("SELECT 4")),
            asyncio.create_task(execute_query("SELECT 5")),
            asyncio.create_task(execute_query("SELECT 6")),
        ]

        await asyncio.gather(*tasks)
```

Child tasks that use database sessions must finish before the owning
``async with db(multi_sessions=True)`` block exits. When ``max_concurrent`` is
set, child tasks should use ``db.connection()`` or pass coroutine objects to
``db.gather()`` so the middleware can own both the session lifetime and the
semaphore slot. Already-created ``Task`` or ``Future`` objects are rejected by
throttled ``db.gather()`` because they may have started outside the semaphore.
