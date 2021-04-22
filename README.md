# FastAPI Async SQLAlchemy middleware

[![ci](https://github.com/h0rn3t/fastapi-async-sqlalchemy/workflows/ci/badge.svg)](https://github.com/h0rn3t/fastapi-async-sqlalchemy/workflows/ci/badge.svg)
[![codecov](https://codecov.io/gh/h0rn3t/fastapi-async-sqlalchemy/branch/main/graph/badge.svg?token=F4NJ34WKPY)](https://codecov.io/gh/h0rn3t/fastapi-async-sqlalchemy)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![pip](https://img.shields.io/pypi/v/fastapi_async_sqlalchemy?color=blue)](https://pypi.org/project/fastapi-async-sqlalchemy/)
[![Downloads](https://pepy.tech/badge/fastapi-async-sqlalchemy)](https://pepy.tech/project/fastapi-async-sqlalchemy)

### Description

FastAPI-Async-SQLAlchemy provides middleware for FastAPI and SQLAlchemy using async AsyncSession and async engine. 
Based on FastAPI-SQLAlchemy

### Install

```bash
  pip install fastapi-async-sqlalchemy
```


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
    db_url="postgresql+asyncpg://user:user@192.168.88.200:5432/primary_db"
)

foo = table("ms_files", column("id"))


@app.get("/")
async def get_files():
    result = await db.session.execute(foo.select())
    return result.fetchall()


@app.get("/db_context")
async def db_context():
    async with db():
        result = await db.session.execute(foo.select())
        return result.fetchall()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)

```