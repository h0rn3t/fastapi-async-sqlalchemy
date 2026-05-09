import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app():
    return FastAPI()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def middleware_pair():
    from fastapi_async_sqlalchemy import create_middleware_and_session_proxy

    return create_middleware_and_session_proxy()


@pytest.fixture
def SQLAlchemyMiddleware(middleware_pair):
    return middleware_pair[0]


@pytest.fixture
def db(middleware_pair):
    return middleware_pair[1]
