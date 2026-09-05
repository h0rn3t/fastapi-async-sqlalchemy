import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from fastapi_async_sqlalchemy.exceptions import (
    MissingSessionError,
    SessionNotInitialisedError,
)

db_url = "sqlite+aiosqlite://"


@pytest.mark.asyncio
async def test_init(app, db, SQLAlchemyMiddleware):
    mw = SQLAlchemyMiddleware(app, db_url=db_url)
    # Pure ASGI middleware: must be callable with (scope, receive, send).
    assert callable(mw)
    assert mw.app is app


@pytest.mark.asyncio
async def test_init_required_args(app, SQLAlchemyMiddleware):
    with pytest.raises(ValueError) as exc_info:
        SQLAlchemyMiddleware(app)

    assert exc_info.value.args[0] == "You need to pass a db_url or a custom_engine parameter."


@pytest.mark.asyncio
async def test_init_required_args_custom_engine(app, db, SQLAlchemyMiddleware):
    custom_engine = create_async_engine(db_url)
    try:
        SQLAlchemyMiddleware(app, custom_engine=custom_engine)
    finally:
        await custom_engine.dispose()


@pytest.mark.asyncio
async def test_init_correct_optional_args(app, db, SQLAlchemyMiddleware):
    engine_args = {"echo": True}

    SQLAlchemyMiddleware(app, db_url, engine_args=engine_args, session_args={})

    async with db():
        assert not db.session.sync_session.expire_on_commit
        engine = db.session.bind
        assert engine.echo

    async with db() as db_ctx:
        engine = db_ctx.session.bind
        assert engine.echo


@pytest.mark.asyncio
async def test_init_incorrect_optional_args(app, SQLAlchemyMiddleware):
    with pytest.raises(TypeError) as exc_info:
        SQLAlchemyMiddleware(app, db_url=db_url, invalid_args="test")

    assert "__init__() got an unexpected keyword argument 'invalid_args'" in exc_info.value.args[0]


@pytest.mark.asyncio
async def test_inside_route(app, db, SQLAlchemyMiddleware):
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    @app.get("/")
    def test_get():
        assert isinstance(db.session, AsyncSession)

    with TestClient(app) as client:
        client.get("/")


@pytest.mark.asyncio
async def test_inside_route_without_middleware_fails(app, client, db):
    @app.get("/")
    def test_get():
        with pytest.raises(SessionNotInitialisedError):
            _ = db.session

    client.get("/")


@pytest.mark.asyncio
async def test_outside_of_route(app, db, SQLAlchemyMiddleware):
    SQLAlchemyMiddleware(app, db_url=db_url)

    async with db():
        assert isinstance(db.session, AsyncSession)


@pytest.mark.asyncio
async def test_outside_of_route_without_middleware_fails(db):
    with pytest.raises(SessionNotInitialisedError):
        _ = db.session

    with pytest.raises(SessionNotInitialisedError):
        async with db():
            pass


@pytest.mark.asyncio
async def test_outside_of_route_without_context_fails(app, db, SQLAlchemyMiddleware):
    SQLAlchemyMiddleware(app, db_url=db_url)

    with pytest.raises(MissingSessionError):
        _ = db.session


@pytest.mark.asyncio
async def test_init_session(app, db, SQLAlchemyMiddleware):
    SQLAlchemyMiddleware(app, db_url=db_url)

    async with db():
        assert isinstance(db.session, AsyncSession)


@pytest.mark.asyncio
async def test_db_session_commit_fail(app, db, SQLAlchemyMiddleware):
    SQLAlchemyMiddleware(app, db_url=db_url, commit_on_exit=True)

    with pytest.raises(IntegrityError):
        async with db():
            raise IntegrityError("test", "test", "test")
        db.session.close.assert_called_once()

    async with db():
        assert db.session


@pytest.mark.asyncio
async def test_rollback(app, db, SQLAlchemyMiddleware):
    #  pytest-cov shows that the line in db.__exit__() rolling back the db session
    #  when there is an Exception is run correctly. However, it would be much better
    #  if we could demonstrate somehow that db.session.rollback() was called e.g. once
    SQLAlchemyMiddleware(app, db_url=db_url)

    with pytest.raises(RuntimeError):
        async with db():
            raise RuntimeError("Test exception")

        db.session.rollback.assert_called_once()


@pytest.mark.parametrize("commit_on_exit", [True, False])
@pytest.mark.asyncio
async def test_db_context_session_args(app, db, SQLAlchemyMiddleware, commit_on_exit):
    SQLAlchemyMiddleware(app, db_url=db_url, commit_on_exit=commit_on_exit)

    session_args = {}

    async with db(session_args=session_args, commit_on_exit=True):
        assert isinstance(db.session, AsyncSession)

    session_args = {"expire_on_commit": False}
    async with db(session_args=session_args):
        _ = db.session


@pytest.mark.asyncio
async def test_multi_sessions(app, db, SQLAlchemyMiddleware):
    SQLAlchemyMiddleware(app, db_url=db_url)

    async with db(multi_sessions=True):

        async def execute_query(query):
            return await db.session.execute(text(query))

        tasks = [
            asyncio.create_task(execute_query("SELECT 1")),
            asyncio.create_task(execute_query("SELECT 2")),
            asyncio.create_task(execute_query("SELECT 3")),
            asyncio.create_task(execute_query("SELECT 4")),
            asyncio.create_task(execute_query("SELECT 5")),
            asyncio.create_task(execute_query("SELECT 6")),
        ]

        res = await asyncio.gather(*tasks)
        assert len(res) == 6


@pytest.mark.asyncio
async def test_concurrent_inserts(app, db, SQLAlchemyMiddleware):
    SQLAlchemyMiddleware(app, db_url=db_url)

    async with db(multi_sessions=True, commit_on_exit=True):
        await db.session.execute(
            text("CREATE TABLE IF NOT EXISTS my_model (id INTEGER PRIMARY KEY, value TEXT)")
        )

        async def insert_data(value):
            await db.session.execute(
                text("INSERT INTO my_model (value) VALUES (:value)"), {"value": value}
            )
            await db.session.flush()

        tasks = [asyncio.create_task(insert_data(f"value_{i}")) for i in range(10)]

        result_ids = await asyncio.gather(*tasks)
        assert len(result_ids) == 10

        records = await db.session.execute(text("SELECT * FROM my_model"))
        records = records.scalars().all()
        assert len(records) == 10


# ---------------------------------------------------------------------------
# `session_args` overriding the defaults the middleware sets itself
#
# `expire_on_commit` and `class_` were passed to `async_sessionmaker` both
# explicitly and again through `**session_args`, so supplying either of them —
# the two most obvious things to configure — failed the middleware's
# construction with `TypeError: got multiple values for keyword argument`.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_args_can_override_expire_on_commit(app, db, SQLAlchemyMiddleware):
    SQLAlchemyMiddleware(app, db_url, session_args={"expire_on_commit": True})

    async with db():
        assert db.session.sync_session.expire_on_commit is True


@pytest.mark.asyncio
async def test_session_args_can_override_the_session_class(app, db, SQLAlchemyMiddleware):
    class CustomSession(AsyncSession):
        pass

    SQLAlchemyMiddleware(app, db_url, session_args={"class_": CustomSession})

    async with db():
        assert isinstance(db.session, CustomSession)


@pytest.mark.asyncio
async def test_session_args_defaults_are_kept_when_not_overridden(app, db, SQLAlchemyMiddleware):
    SQLAlchemyMiddleware(app, db_url, session_args={"autoflush": False})

    async with db():
        session = db.session
        assert session.sync_session.expire_on_commit is False
        assert session.sync_session.autoflush is False
