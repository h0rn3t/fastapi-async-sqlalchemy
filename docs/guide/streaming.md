# Streaming Responses

A `StreamingResponse` (or `FileResponse`) has a **different lifetime** from a
normal request transaction. The body keeps yielding chunks *after* your route
function returns, but the middleware-managed request session is tied to the
request transaction — not to the stream. So you must not rely on `db.session`
staying open while a streaming body runs.

The rule: **open an explicit session inside the generator** so the body owns its
own database lifetime.

## The right way

```python
from fastapi.responses import StreamingResponse
from fastapi_async_sqlalchemy import db

@app.get("/export")
async def export():
    async def rows():
        async with db():  # body-owned session
            result = await db.session.stream(foo.select())
            async for row in result:
                yield f"{row.id}\n".encode()

    return StreamingResponse(rows(), media_type="text/plain")
```

The `async with db()` inside the generator makes the session lifetime explicit
and keeps the session open for the whole body.

## Why not `commit_on_exit=True`?

Implicit `commit_on_exit=True` is **not** a safe way to report streaming write
success. The response may have already started — and early chunks already sent —
before an unbounded body finishes. A late commit failure cannot un-send those
chunks.

To enforce this, the middleware actively rejects the unsafe combination. If a
streaming response begins while `commit_on_exit=True` **and** the request
session was already used, it raises:

```text
RuntimeError: `commit_on_exit=True` cannot use the middleware-managed request
database session with a streaming response. Use `async with db()` inside the
streaming generator, or manage the streaming transaction explicitly.
```

Similarly, once the request session has been closed for streaming, touching it
again raises a `RuntimeError` telling you to use `async with db()` inside the
generator.

## If a streaming route needs to write

Pick one of two explicit patterns:

=== "Commit before streaming"

    Complete and commit the write in its own context **before** creating the
    streaming response, then stream read-only:

    ```python
    @app.post("/report")
    async def make_report():
        async with db(commit_on_exit=True):
            db.session.add(ReportRun(status="started"))
            # committed here, before any streaming begins

        async def body():
            async with db():
                result = await db.session.stream(rows.select())
                async for row in result:
                    yield serialize(row)

        return StreamingResponse(body())
    ```

=== "Write inside the generator"

    Make the generator own an explicit write transaction and design the API so
    clients don't treat early chunks as confirmation of a completed write:

    ```python
    @app.get("/stream-and-write")
    async def stream_and_write():
        async def body():
            async with db(commit_on_exit=True):
                async for row in produce():
                    db.session.add(AuditRow(data=row))
                    yield row
                # committed when the generator's context exits

        return StreamingResponse(body())
    ```

## Migrating existing code

If you previously used `db.session` directly inside a streaming generator, move
that code into a generator-owned `async with db()` context as shown above. This
keeps database access available for the whole body while making it clear that
the session lifetime belongs to the stream, not the original request
transaction.
