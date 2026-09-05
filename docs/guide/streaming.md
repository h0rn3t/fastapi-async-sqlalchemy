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

## What the middleware does when a body starts flowing

As soon as the first chunk of a chunked body arrives, the middleware finalizes
the request session — committing it when `commit_on_exit=True`, rolling it back
otherwise — and closes it. That happens *before* the buffered
`http.response.start` is forwarded, so a failing commit still turns the response
into a 500 rather than a 200 whose writes were silently lost.

Touching the session after that point raises:

```text
RuntimeError: The middleware-managed request database session is closed for
streaming response body generation. Use `async with db()` inside the streaming
generator to make the session lifetime explicit.
```

That error is the enforcement: implicit `commit_on_exit=True` is **not** a safe
way to report streaming write success, because early chunks may already be on
the wire before an unbounded body finishes, and a late commit failure cannot
un-send them.

!!! note "Why the middleware does not try to detect 'real' streaming"
    A chunked body and a finished response cannot be told apart at the ASGI
    level. `BaseHTTPMiddleware` — what every `@app.middleware("http")` becomes —
    re-emits even a fully buffered response as one chunk flagged
    `more_body=True` plus a terminating empty one, and a compressing middleware
    below it drops the `content-length` that would otherwise have settled the
    question. So the middleware does not guess: it finalizes, and tells you if
    you then reach for the session.

A transaction your own code owns — `async with db.session.begin()` in a `yield`
dependency — is the one exception. It is left alone and finalized by its owner;
see [`yield` dependencies that own a transaction](http-load.md#yield-dependencies-that-own-a-transaction).

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
