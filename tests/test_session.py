import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from starlette.middleware.base import BaseHTTPMiddleware

from fastapi_async_sqlalchemy.exceptions import MissingSessionError, SessionNotInitialisedError

db_url = "sqlite+aiosqlite://"


@pytest.mark.asyncio
async def test_init(app, SQLAlchemyMiddleware):
    mw = SQLAlchemyMiddleware(app, db_url=db_url)
    assert isinstance(mw, BaseHTTPMiddleware)


@pytest.mark.asyncio
async def test_init_required_args(app, SQLAlchemyMiddleware):
    with pytest.raises(ValueError) as exc_info:
        SQLAlchemyMiddleware(app)

    assert exc_info.value.args[0] == "You need to pass a db_url or a custom_engine parameter."


@pytest.mark.asyncio
async def test_init_required_args_custom_engine(app, db, SQLAlchemyMiddleware):
    custom_engine = create_async_engine(db_url)
    SQLAlchemyMiddleware(app, custom_engine=custom_engine)


@pytest.mark.asyncio
async def test_init_correct_optional_args(app, db, SQLAlchemyMiddleware):
    engine_args = {"echo": True}
    # session_args = {"expire_on_commit": False}

    SQLAlchemyMiddleware(app, db_url, engine_args=engine_args, session_args={})

    async with db():
        # assert not db.session.expire_on_commit
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
async def test_inside_route(app, client, db, SQLAlchemyMiddleware):
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    @app.get("/")
    def test_get():
        assert isinstance(db.session, AsyncSession)

    client.get("/")


@pytest.mark.asyncio
async def test_inside_route_without_middleware_fails(app, client, db):
    @app.get("/")
    def test_get():
        with pytest.raises(SessionNotInitialisedError):
            db.session

    client.get("/")


@pytest.mark.asyncio
async def test_outside_of_route(app, db, SQLAlchemyMiddleware):
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db():
        assert isinstance(db.session, AsyncSession)


@pytest.mark.asyncio
async def test_outside_of_route_without_middleware_fails(db):
    with pytest.raises(SessionNotInitialisedError):
        db.session

    with pytest.raises(SessionNotInitialisedError):
        async with db():
            pass


@pytest.mark.asyncio
async def test_outside_of_route_without_context_fails(app, db, SQLAlchemyMiddleware):
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    with pytest.raises(MissingSessionError):
        db.session


@pytest.mark.asyncio
async def test_init_session(app, db, SQLAlchemyMiddleware):
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db():
        assert isinstance(db.session, AsyncSession)


@pytest.mark.asyncio
async def test_db_session_commit_fail(app, db, SQLAlchemyMiddleware):
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url, commit_on_exit=True)

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
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    with pytest.raises(Exception):
        async with db():
            raise Exception

        db.session.rollback.assert_called_once()


@pytest.mark.parametrize("commit_on_exit", [True, False])
@pytest.mark.asyncio
async def test_db_context_session_args(app, db, SQLAlchemyMiddleware, commit_on_exit):
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url, commit_on_exit=commit_on_exit)

    session_args = {}

    async with db(session_args=session_args, commit_on_exit=True):
        assert isinstance(db.session, AsyncSession)

    session_args = {"expire_on_commit": False}
    async with db(session_args=session_args):
        db.session


@pytest.mark.asyncio
async def test_multi_sessions(app, db, SQLAlchemyMiddleware):
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

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
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

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
