"""
SQLAlchemy Events Example with fastapi-async-sqlalchemy

This example demonstrates how to use SQLAlchemy's event system
for common use cases like validation, timestamps, and audit logging.
"""

from datetime import datetime

from fastapi import FastAPI, HTTPException
from sqlalchemy import Boolean, Column, DateTime, Integer, String, event
from sqlalchemy.orm import DeclarativeBase

from fastapi_async_sqlalchemy import SQLAlchemyMiddleware, db


# Define base
class Base(DeclarativeBase):
    pass


# User model with events
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False)
    email = Column(String(100), unique=True, nullable=False)
    full_name = Column(String(100))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


# ============================================================================
# EVENT LISTENERS
# ============================================================================


# 1. Data Normalization (before insert)
# ----------------------------------------------------------------------------
@event.listens_for(User, "before_insert")
def normalize_user_data(mapper, connection, target):
    """Normalize user data before inserting into database"""
    target.username = target.username.lower().strip()
    target.email = target.email.lower().strip()

    if target.full_name:
        target.full_name = target.full_name.title().strip()


# 2. Validation (before insert and update)
# ----------------------------------------------------------------------------
@event.listens_for(User, "before_insert")
@event.listens_for(User, "before_update")
def validate_user(mapper, connection, target):
    """Validate user data before saving"""
    # Validate username
    if not target.username or len(target.username) < 3:
        raise ValueError("Username must be at least 3 characters long")

    if not target.username.replace("_", "").isalnum():
        raise ValueError("Username can only contain letters, numbers, and underscores")

    # Validate email
    if not target.email or "@" not in target.email or "." not in target.email:
        raise ValueError("Invalid email address")


# 3. Automatic Timestamps (before update)
# ----------------------------------------------------------------------------
@event.listens_for(User, "before_update")
def update_timestamp(mapper, connection, target):
    """Automatically update the updated_at timestamp on every update"""
    target.updated_at = datetime.utcnow()


# 4. Logging (after insert/update/delete)
# ----------------------------------------------------------------------------
@event.listens_for(User, "after_insert")
def log_user_created(mapper, connection, target):
    """Log when a new user is created"""
    print(f"✅ USER CREATED: {target.username} (ID: {target.id}) at {target.created_at}")


@event.listens_for(User, "after_update")
def log_user_updated(mapper, connection, target):
    """Log when a user is updated"""
    print(f"✏️  USER UPDATED: {target.username} (ID: {target.id}) at {target.updated_at}")


@event.listens_for(User, "after_delete")
def log_user_deleted(mapper, connection, target):
    """Log when a user is deleted"""
    print(f"🗑️  USER DELETED: {target.username} (ID: {target.id})")


# 5. Prevent Hard Delete (optional - implement soft delete)
# ----------------------------------------------------------------------------
# Uncomment to enable soft delete instead of hard delete
#
# @event.listens_for(User, "before_delete")
# def prevent_hard_delete(mapper, connection, target):
#     """Prevent hard delete - use soft delete instead"""
#     raise Exception("Hard delete not allowed. Use soft delete (set is_active=False)")


# ============================================================================
# FASTAPI APPLICATION
# ============================================================================

app = FastAPI(title="SQLAlchemy Events Example")

# Add middleware
app.add_middleware(
    SQLAlchemyMiddleware,
    db_url="sqlite+aiosqlite:///./events_example.db",
    commit_on_exit=False,  # We'll commit manually
)


# ============================================================================
# API ENDPOINTS
# ============================================================================


@app.on_event("startup")
async def create_tables():
    """Create database tables on startup"""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///./events_example.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


@app.post("/users", response_model=dict)
async def create_user(username: str, email: str, full_name: str = None):
    """
    Create a new user.

    Events triggered:
    - before_insert: Data normalization and validation
    - after_insert: Logging
    """
    async with db():
        try:
            user = User(username=username, email=email, full_name=full_name)
            db.session.add(user)
            await db.session.commit()

            return {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "full_name": user.full_name,
                "created_at": user.created_at.isoformat(),
                "message": "User created successfully! Check console for event logs.",
            }
        except ValueError as e:
            await db.session.rollback()
            raise HTTPException(status_code=400, detail=f"Validation error: {str(e)}") from e
        except Exception as e:
            await db.session.rollback()
            raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/users/{user_id}")
async def get_user(user_id: int):
    """Get user by ID"""
    async with db():
        user = await db.session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        return {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "full_name": user.full_name,
            "is_active": user.is_active,
            "created_at": user.created_at.isoformat(),
            "updated_at": user.updated_at.isoformat(),
        }


@app.put("/users/{user_id}")
async def update_user(user_id: int, username: str = None, email: str = None, full_name: str = None):
    """
    Update user information.

    Events triggered:
    - before_update: Validation and automatic timestamp update
    - after_update: Logging
    """
    async with db():
        user = await db.session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        try:
            # Update fields if provided
            if username is not None:
                user.username = username
            if email is not None:
                user.email = email
            if full_name is not None:
                user.full_name = full_name

            await db.session.commit()

            return {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "full_name": user.full_name,
                "updated_at": user.updated_at.isoformat(),
                "message": "User updated successfully! updated_at was set automatically.",
            }
        except ValueError as e:
            await db.session.rollback()
            raise HTTPException(status_code=400, detail=f"Validation error: {str(e)}") from e


@app.delete("/users/{user_id}")
async def delete_user(user_id: int):
    """
    Delete a user.

    Events triggered:
    - before_delete: (could prevent deletion or archive data)
    - after_delete: Logging
    """
    async with db():
        user = await db.session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        username = user.username  # Save for response

        try:
            await db.session.delete(user)
            await db.session.commit()

            return {
                "message": f"User {username} deleted successfully! Check console for event logs."
            }
        except Exception as e:
            await db.session.rollback()
            raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/users/{user_id}/soft-delete")
async def soft_delete_user(user_id: int):
    """
    Soft delete a user (set is_active to False).

    This triggers update events instead of delete events.
    """
    async with db():
        user = await db.session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        user.is_active = False
        await db.session.commit()

        return {
            "message": f"User {user.username} soft-deleted (is_active=False)",
            "updated_at": user.updated_at.isoformat(),
        }


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    """
    Run this example:

    1. Install dependencies:
       pip install fastapi uvicorn sqlalchemy aiosqlite

    2. Run the application:
       python events_example.py

    3. Test in another terminal:
       # Create user (watch console for event logs)
       curl -X POST "http://localhost:8000/users" \\
         -H "Content-Type: application/json" \\
         -d '{"username":"JohnDoe", "email":"JOHN@EXAMPLE.COM", "full_name":"john doe"}'

       # Get user
       curl "http://localhost:8000/users/1"

       # Update user
       curl -X PUT "http://localhost:8000/users/1" \\
         -H "Content-Type: application/json" \\
         -d '{"email":"newemail@example.com"}'

       # Soft delete
       curl -X POST "http://localhost:8000/users/1/soft-delete"

       # Delete user
       curl -X DELETE "http://localhost:8000/users/1"

    Expected console output:
    ✅ USER CREATED: johndoe (ID: 1) at 2024-01-01 12:00:00
    ✏️  USER UPDATED: johndoe (ID: 1) at 2024-01-01 12:05:00
    🗑️  USER DELETED: johndoe (ID: 1)
    """

    import uvicorn

    print("\n" + "=" * 70)
    print("SQLAlchemy Events Example with fastapi-async-sqlalchemy")
    print("=" * 70)
    print("\nEvents registered:")
    print("  ✓ before_insert: Data normalization")
    print("  ✓ before_insert/update: Validation")
    print("  ✓ before_update: Automatic timestamp update")
    print("  ✓ after_insert: Create logging")
    print("  ✓ after_update: Update logging")
    print("  ✓ after_delete: Delete logging")
    print("\nStarting server on http://localhost:8000")
    print("API docs at http://localhost:8000/docs")
    print("\nWatch this console for event logs!\n")
    print("=" * 70 + "\n")

    uvicorn.run(app, host="0.0.0.0", port=8000)
