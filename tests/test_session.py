from unittest.mock import Mock, patch
import pytest
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware

from fastapi_async_sqlalchemy.exceptions import MissingSessionError, SessionNotInitialisedError

db_url = "sqlite+aiosqlite://"


@pytest.mark.asyncio
def test_init(app, SQLAlchemyMiddleware):
    mw = SQLAlchemyMiddleware(app, db_url=db_url)
    assert isinstance(mw, BaseHTTPMiddleware)


@pytest.mark.asyncio
def test_init_required_args(app, SQLAlchemyMiddleware):
    with pytest.raises(ValueError) as exc_info:
        SQLAlchemyMiddleware(app)

    assert exc_info.value.args[0] == "You need to pass a db_url or a custom_engine parameter."


def test_init_required_args_custom_engine(app, db, SQLAlchemyMiddleware):
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


@pytest.mark.asyncio
def test_init_incorrect_optional_args(app, SQLAlchemyMiddleware):
    with pytest.raises(TypeError) as exc_info:
        SQLAlchemyMiddleware(app, db_url=db_url, invalid_args="test")

    assert exc_info.value.args[0] == "__init__() got an unexpected keyword argument 'invalid_args'"


@pytest.mark.asyncio
def test_inside_route(app, client, db, SQLAlchemyMiddleware):
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    @app.get("/")
    def test_get():
        assert isinstance(db.session, AsyncSession)

    client.get("/")


@pytest.mark.asyncio
def test_inside_route_without_middleware_fails(app, client, db):
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
def test_outside_of_route_without_context_fails(app, db, SQLAlchemyMiddleware):
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    with pytest.raises(MissingSessionError):
        db.session


@pytest.mark.parametrize("commit_on_exit", [True, False])
@pytest.mark.asyncio
async def test_db_context_session_args(app, db, SQLAlchemyMiddleware, commit_on_exit):
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url, commit_on_exit=commit_on_exit)

    session_args = {}

    async with db(session_args=session_args):
        assert isinstance(db.session, AsyncSession)

        # assert db.session.expire_on_commit

    session_args = {"expire_on_commit": False}
    async with db(session_args=session_args):
        db.session
        #assert db.session.expire_on_commit


@pytest.mark.asyncio
async def test_rollback(app, db, SQLAlchemyMiddleware):
    #  pytest-cov shows that the line in db.__exit__() rolling back the db session
    #  when there is an Exception is run correctly. However, it would be much better
    #  if we could demonstrate somehow that db.session.rollback() was called e.g. once
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    with pytest.raises(Exception):
        async with db():
            raise Exception
