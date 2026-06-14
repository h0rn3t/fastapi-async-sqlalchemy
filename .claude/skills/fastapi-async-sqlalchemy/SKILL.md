```markdown
# fastapi-async-sqlalchemy Development Patterns

> Auto-generated skill from repository analysis

## Overview
This skill covers the development patterns and conventions used in the `fastapi-async-sqlalchemy` repository, a Python codebase focused on integrating asynchronous SQLAlchemy patterns, likely for use with FastAPI or similar frameworks. It details file organization, import/export styles, commit conventions, and test structuring to help contributors write consistent, maintainable code.

## Coding Conventions

### File Naming
- Use **snake_case** for all file names.
  - Example: `database_utils.py`, `user_model.py`

### Import Style
- Use **relative imports** within the codebase.
  - Example:
    ```python
    from .models import User
    from ..utils import get_db_session
    ```

### Export Style
- Use **named exports** (explicitly define what is exported from modules).
  - Example:
    ```python
    __all__ = ["User", "get_db_session"]
    ```

### Commit Patterns
- Commit messages are **freeform** (no enforced prefix or type).
- Average commit message length: ~27 characters.

## Workflows

### Adding a New Database Model
**Trigger:** When you need to define a new table/entity.
**Command:** `/add-model`

1. Create a new file in the models directory using snake_case (e.g., `order_model.py`).
2. Define your SQLAlchemy model class.
    ```python
    from sqlalchemy import Column, Integer, String
    from .base import Base

    class Order(Base):
        __tablename__ = "orders"
        id = Column(Integer, primary_key=True)
        description = Column(String)
    ```
3. Add the model to the module's `__all__` for named export.
4. Use relative imports to access the model elsewhere.

### Writing Asynchronous Database Operations
**Trigger:** When implementing async CRUD or queries.
**Command:** `/add-async-operation`

1. Use `async def` for all database operation functions.
2. Use SQLAlchemy's async session patterns.
    ```python
    async def get_order_by_id(session, order_id: int):
        result = await session.execute(
            select(Order).where(Order.id == order_id)
        )
        return result.scalar_one_or_none()
    ```
3. Import models and utilities using relative imports.

### Running Tests
**Trigger:** When validating code changes.
**Command:** `/run-tests`

1. Locate test files matching the `*.test.*` pattern (e.g., `order.test.py`).
2. Use the project's preferred test runner (framework not specified).
3. Run tests and ensure all pass before committing.

## Testing Patterns

- Test files are named with the pattern `*.test.*` (e.g., `user.test.py`).
- The specific testing framework is **unknown**; check for test runner configuration or use common Python test runners (pytest, unittest).
- Place test files alongside or within a `tests` directory.
- Write tests that cover async database operations and model behaviors.

## Commands
| Command            | Purpose                                      |
|--------------------|----------------------------------------------|
| /add-model         | Scaffold a new database model                |
| /add-async-operation | Add a new async database operation         |
| /run-tests         | Run the test suite                           |
```
