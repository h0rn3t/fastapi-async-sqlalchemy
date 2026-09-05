# SQLAlchemy Events

SQLAlchemy's [event system](https://docs.sqlalchemy.org/en/20/orm/events.html)
is **independent of the session and engine**. This middleware doesn't change how
events fire — register listeners on your mapped classes (or on `Mapper` /
`Session`) with `sqlalchemy.event.listens_for` exactly as you would in a
synchronous SQLAlchemy setup.

## Registering listeners

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

These fire when the session flushes, just like always.

## :warning: Mapper events are synchronous

Mapper-level events receive a **synchronous** `connection` argument:

`before_insert` · `after_insert` · `before_update` · `after_update` ·
`before_delete` · `after_delete`

Inside these handlers:

- **Do not** `await` anything.
- **Do not** call async ORM APIs.

```python
@event.listens_for(User, "before_insert")
def handler(mapper, connection, target):
    # ✅ pure, synchronous work on `target`
    target.username = target.username.strip().lower()
    # ❌ await db.session.execute(...)   <- not allowed here
```

If you need async work after a write, do it **after** `await
db.session.commit()` returns, or use `Session`-level events such as
`after_flush` / `after_commit` and schedule the async work from there.

## A complete example

A runnable example with validation, automatic timestamps, audit logging, and a
commented-out soft-delete hook lives in the repository at
[`examples/events_example.py`](https://github.com/h0rn3t/fastapi-async-sqlalchemy/blob/main/examples/events_example.py).
It wires the listeners above into a small FastAPI CRUD app:

```python
@app.post("/users")
async def create_user(username: str, email: str, full_name: str | None = None):
    async with db():
        user = User(username=username, email=email, full_name=full_name)
        db.session.add(user)
        await db.session.commit()  # before_insert / after_insert fire on flush
        return {"id": user.id, "username": user.username}
```
