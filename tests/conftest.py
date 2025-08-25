import sys

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient


@pytest.fixture
def app():
    return FastAPI()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def SQLAlchemyMiddleware():
    from fastapi_async_sqlalchemy import SQLAlchemyMiddleware

    yield SQLAlchemyMiddleware


@pytest.fixture
def db():
    from fastapi_async_sqlalchemy import db

    yield db

    # force reloading of module to clear global state

    try:
        del sys.modules["fastapi_async_sqlalchemy"]
    except KeyError:
        pass

    try:
        del sys.modules["fastapi_async_sqlalchemy.middleware"]
    except KeyError:
        pass
