from typing import Optional

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Try to import SQLModel and related components
try:
    from sqlmodel import Field, SQLModel, select
    from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

    SQLMODEL_AVAILABLE = True
except ImportError:
    SQLMODEL_AVAILABLE = False
    SQLModel = None
    Field = None
    select = None
    SQLModelAsyncSession = None

db_url = "sqlite+aiosqlite://"


# Define test models only if SQLModel is available
if SQLMODEL_AVAILABLE:

    class Hero(SQLModel, table=True):  # type: ignore
        __tablename__ = "test_hero"

        id: Optional[int] = Field(default=None, primary_key=True)
        name: str = Field(index=True)
        secret_name: str
        age: Optional[int] = Field(default=None, index=True)


@pytest.mark.skipif(not SQLMODEL_AVAILABLE, reason="SQLModel not available")
@pytest.mark.asyncio
async def test_sqlmodel_session_type(app, db, SQLAlchemyMiddleware):
    """Test that SQLModel's AsyncSession is used when SQLModel is available"""
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db():
        # Should be SQLModel's AsyncSession, not regular SQLAlchemy AsyncSession
        assert isinstance(db.session, SQLModelAsyncSession)
        assert hasattr(db.session, "exec")


@pytest.mark.skipif(not SQLMODEL_AVAILABLE, reason="SQLModel not available")
@pytest.mark.asyncio
async def test_sqlmodel_exec_method_exists(app, db, SQLAlchemyMiddleware):
    """Test that the .exec() method is available on the session"""
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db():
        # Test that exec method exists
        assert hasattr(db.session, "exec")
        assert callable(db.session.exec)


@pytest.mark.skipif(not SQLMODEL_AVAILABLE, reason="SQLModel not available")
@pytest.mark.asyncio
async def test_sqlmodel_exec_method_basic_query(app, db, SQLAlchemyMiddleware):
    """Test that the .exec() method works with basic SQLModel queries"""
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db():
        # Create tables using the session's bind engine
        async with db.session.bind.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

        # Test basic select query with exec
        query = select(Hero)
        result = await db.session.exec(query)
        heroes = result.all()
        assert isinstance(heroes, list)
        assert len(heroes) == 0  # Should be empty initially


@pytest.mark.skipif(not SQLMODEL_AVAILABLE, reason="SQLModel not available")
@pytest.mark.asyncio
async def test_sqlmodel_exec_crud_operations(app, db, SQLAlchemyMiddleware):
    """Test CRUD operations using SQLModel with .exec() method"""
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db(commit_on_exit=True):
        # Create tables using the session's bind engine
        async with db.session.bind.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        # Create a hero
        hero = Hero(name="Spider-Man", secret_name="Peter Parker", age=25)
        db.session.add(hero)
        await db.session.commit()
        await db.session.refresh(hero)

        # Test that hero was created and has an ID
        assert hero.id is not None

        # Query the hero using exec
        query = select(Hero).where(Hero.name == "Spider-Man")
        result = await db.session.exec(query)
        found_hero = result.first()

        assert found_hero is not None
        assert isinstance(found_hero, Hero)  # Should be SQLModel instance, not Row
        assert found_hero.name == "Spider-Man"
        assert found_hero.secret_name == "Peter Parker"
        assert found_hero.age == 25


@pytest.mark.skipif(not SQLMODEL_AVAILABLE, reason="SQLModel not available")
@pytest.mark.asyncio
async def test_sqlmodel_exec_with_where_clause(app, db, SQLAlchemyMiddleware):
    """Test .exec() method with WHERE clauses"""
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db(commit_on_exit=True):
        # Create tables using the session's bind engine
        async with db.session.bind.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        # Create multiple heroes
        heroes_data = [
            Hero(name="Spider-Man", secret_name="Peter Parker", age=25),
            Hero(name="Iron Man", secret_name="Tony Stark", age=45),
            Hero(name="Captain America", secret_name="Steve Rogers", age=100),
        ]

        for hero in heroes_data:
            db.session.add(hero)
        await db.session.commit()

        # Test filtering by age
        query = select(Hero).where(Hero.age > 30)
        result = await db.session.exec(query)
        older_heroes = result.all()

        assert len(older_heroes) == 2
        hero_names = [hero.name for hero in older_heroes]
        assert "Iron Man" in hero_names
        assert "Captain America" in hero_names
        assert "Spider-Man" not in hero_names


@pytest.mark.skipif(not SQLMODEL_AVAILABLE, reason="SQLModel not available")
@pytest.mark.asyncio
async def test_sqlmodel_exec_returns_sqlmodel_objects(app, db, SQLAlchemyMiddleware):
    """Test that .exec() returns actual SQLModel objects, not Row objects"""
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db(commit_on_exit=True):
        # Create tables using the session's bind engine
        async with db.session.bind.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        # Create a hero
        hero = Hero(name="Batman", secret_name="Bruce Wayne", age=35)
        db.session.add(hero)
        await db.session.commit()
        await db.session.refresh(hero)

        # Query using exec
        query = select(Hero).where(Hero.name == "Batman")
        result = await db.session.exec(query)
        found_hero = result.first()

        # Should be a SQLModel instance, not a Row
        assert isinstance(found_hero, Hero)
        assert isinstance(found_hero, SQLModel)
        assert not str(type(found_hero)).startswith("<class 'sqlalchemy.engine.row.Row")

        # Should have all the SQLModel methods
        assert hasattr(found_hero, "model_dump")
        assert found_hero.name == "Batman"


@pytest.mark.asyncio
async def test_backward_compatibility_with_regular_execute(app, db, SQLAlchemyMiddleware):
    """Test that regular SQLAlchemy .execute() method still works for backward compatibility"""
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db():
        # Test regular execute with text query
        result = await db.session.execute(text("SELECT 1 as test_value"))
        row = result.fetchone()
        assert row is not None
        assert row[0] == 1


@pytest.mark.asyncio
async def test_session_type_without_sqlmodel(app, db, SQLAlchemyMiddleware):
    """Test that when SQLModel is not available, regular AsyncSession is used"""
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db():
        # Should still be an AsyncSession (either SQLModel or regular)
        assert isinstance(db.session, AsyncSession)

        # Regular execute should always work
        result = await db.session.execute(text("SELECT 2 as test_value"))
        row = result.fetchone()
        assert row is not None
        assert row[0] == 2


@pytest.mark.skipif(not SQLMODEL_AVAILABLE, reason="SQLModel not available")
@pytest.mark.asyncio
async def test_sqlmodel_exec_in_route(app, client, db, SQLAlchemyMiddleware):
    """Test SQLModel .exec() method works inside FastAPI routes"""
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    @app.get("/test-sqlmodel")
    async def test_route():
        # Create tables using the session's bind engine
        async with db.session.bind.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

        # Create and query a hero using exec
        hero = Hero(name="Flash", secret_name="Barry Allen", age=28)
        db.session.add(hero)
        await db.session.commit()
        await db.session.refresh(hero)

        query = select(Hero).where(Hero.name == "Flash")
        result = await db.session.exec(query)
        found_hero = result.first()

        return {
            "found": found_hero is not None,
            "is_sqlmodel": isinstance(found_hero, SQLModel),
            "name": found_hero.name if found_hero else None,
        }

    response = client.get("/test-sqlmodel")
    data = response.json()
    assert data["found"] is True
    assert data["is_sqlmodel"] is True
    assert data["name"] == "Flash"


@pytest.mark.skipif(not SQLMODEL_AVAILABLE, reason="SQLModel not available")
@pytest.mark.asyncio
async def test_sqlmodel_exec_multi_sessions(app, db, SQLAlchemyMiddleware):
    """Test SQLModel .exec() method works with multi_sessions=True"""
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db(multi_sessions=True):
        # Create tables using the session's bind engine
        async with db.session.bind.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

        # Test that each session access gets a new session with exec method
        session1 = db.session
        session2 = db.session
        session3 = db.session

        # All sessions should have exec method
        assert hasattr(session1, "exec")
        assert hasattr(session2, "exec")
        assert hasattr(session3, "exec")

        # Test basic exec query on one session
        query = select(Hero)
        result = await session1.exec(query)
        heroes = result.all()
        assert isinstance(heroes, list)


@pytest.mark.skipif(not SQLMODEL_AVAILABLE, reason="SQLModel not available")
@pytest.mark.asyncio
async def test_sqlmodel_session_has_both_exec_and_execute(app, db, SQLAlchemyMiddleware):
    """Test that SQLModel session has both .exec() and .execute() methods"""
    app.add_middleware(SQLAlchemyMiddleware, db_url=db_url)

    async with db():
        # Should have both methods
        assert hasattr(db.session, "exec")
        assert hasattr(db.session, "execute")
        assert callable(db.session.exec)
        assert callable(db.session.execute)

        # Both should work
        result1 = await db.session.execute(text("SELECT 42 as answer"))
        row1 = result1.fetchone()
        assert row1[0] == 42

        # exec should work too (though with text it's similar to execute)
        result2 = await db.session.exec(text("SELECT 24 as answer"))
        row2 = result2.fetchone()
        assert row2[0] == 24
