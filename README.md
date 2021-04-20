# FastAPI Async SQLAlchemy middleware

[![ci](https://github.com/h0rn3t/fastapi-async-sqlalchemy/workflows/ci/badge.svg)](https://github.com/h0rn3t/fastapi-async-sqlalchemy/workflows/ci/badge.svg)
[![codecov](https://codecov.io/gh/h0rn3t/fastapi-async-sqlalchemy/branch/main/graph/badge.svg?token=F4NJ34WKPY)](https://codecov.io/gh/h0rn3t/fastapi-async-sqlalchemy)
[![License](https://img.shields.io/npm/l/xxtea-node.svg)](http://opensource.org/licenses/MIT)
[![pip](https://img.shields.io/pypi/v/fastapi_async_sqlalchemy?color=blue)](https://img.shields.io/pypi/v/fastapi_async_sqlalchemy?color=blue)

### Description

FastAPI-Async-SQLAlchemy provides middleware for FastAPI and SQLAlchemy using async AsyncSession and async engine. 
Based on FastAPI-SQLAlchemy

### Install

```bash
  pip install fastapi-async-sqlalchemy
```


### Examples


###Usage inside of a route

Note that the session object provided by ``db.session`` is based on the Python3.7+ ``ContextVar``. This means that
each session is linked to the individual request context in which it was created.

```python

    from fastapi import FastAPI
    from fastapi_async_sqlalchemy import SQLAlchemyMiddleware  # middleware helper
    from fastapi_async_sqlalchemy import db  # an object to provide global access to a database session

    from app.models import User

    app = FastAPI()

    app.add_middleware(SQLAlchemyMiddleware, db_url="sqlite+aiosqlite://")

    # once the middleware is applied, any route can then access the database session 
    # from the global ``db``

    @app.get("/users")
    def get_users():
        users = db.session.query(User).all()

        return users
```


###Usage outside of a route

Sometimes it is useful to be able to access the database outside the context of a request, such as in scheduled tasks which run in the background:

```python

    import pytz
    from apscheduler.schedulers.asyncio import AsyncIOScheduler  # other schedulers are available
    from fastapi import FastAPI
    from fastapi_async_sqlalchemy import db

    from app.models import User, UserCount

    app = FastAPI()

    app.add_middleware(DBSessionMiddleware, db_url="sqlite+aiosqlite://")


    @app.on_event('startup')
    async def startup_event():
        scheduler = AsyncIOScheduler(timezone=pytz.utc)
        scheduler.start()
        scheduler.add_job(count_users_task, "cron", hour=0)  # runs every night at midnight


    def count_users_task():
        """Count the number of users in the database and save it into the user_counts table."""

        # we are outside of a request context, therefore we cannot rely on ``DBSessionMiddleware``
        # to create a database session for us. Instead, we can use the same ``db`` object and 
        # use it as a context manager, like so:

        with db():
            user_count = db.session.query(User).count()

            db.session.add(UserCount(user_count))
            db.session.commit()
        
        # no longer able to access a database session once the db() context manager has ended

        return users
```
