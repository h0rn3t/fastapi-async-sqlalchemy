# Type Hints

The package ships a `py.typed` marker, so type checkers read its inline
annotations. The one piece worth knowing about is how to annotate the `db`
proxy itself.

## Annotating `db` with `DBSessionMeta`

Use `DBSessionMeta` when you need to type a
function or attribute that holds the `db` proxy:

```python
from fastapi_async_sqlalchemy import DBSessionMeta, db


def get_db() -> DBSessionMeta:
    return db
```

This gives static checkers (mypy, pyright) and your IDE full autocomplete for
the proxy surface — `session`, `connection()`, `gather()` and the `db(...)`
call.

## Runtime vs. type-check behavior

`DBSessionMeta` is deliberately two things at once:

- **At runtime** it is the actual metaclass of `db`, so identity and instance
  checks work as they did in earlier versions:

  ```python
  from fastapi_async_sqlalchemy import DBSessionMeta, db

  assert isinstance(db, DBSessionMeta)
  assert type(db) is DBSessionMeta
  ```

- **At type-check time** it resolves to a structural
  [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)
  describing the public API. That's what powers autocomplete and `mypy`
  checking when you annotate with it.

The Protocol surface is:

```python
class DBSessionMeta(Protocol):
    @property
    def session(self) -> AsyncSession: ...

    def connection(self) -> AbstractAsyncContextManager[AsyncSession]: ...

    async def gather(self, *coros_or_futures: Any, return_exceptions: bool = ...) -> list[Any]: ...

    def __call__(
        self,
        session_args: dict[str, Any] | None = ...,
        commit_on_exit: bool = ...,
        multi_sessions: bool = ...,
        max_concurrent: int | None = ...,
    ) -> AbstractAsyncContextManager[Any]: ...
```

## Dependency-injection style

If you prefer passing `db` explicitly (e.g. for testability) rather than
importing the global, the annotation makes it first-class:

```python
from fastapi import Depends
from fastapi_async_sqlalchemy import DBSessionMeta, db


def get_db() -> DBSessionMeta:
    return db


async def list_users(database: DBSessionMeta = Depends(get_db)):
    result = await database.session.execute(users.select())
    return result.fetchall()
```

## Works with SQLModel

If `sqlmodel` is installed, the middleware uses `sqlmodel`'s `AsyncSession`
subclass automatically, so `db.session` exposes SQLModel's session API. No extra
configuration is needed.
