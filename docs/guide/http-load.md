# HTTP Load & Background Tasks

## Limit simultaneous requests

Use admission control to give excess HTTP traffic a short queue deadline before
it enters a route or starts a database transaction:

```python
app.add_middleware(
    SQLAlchemyMiddleware,
    db_url=DB_URL,
    engine_args={"pool_size": 20, "max_overflow": 50, "pool_timeout": 5},
    max_concurrent_requests=50,
    request_queue_timeout=0.1,
    exclude_paths={"/health", "/health/live"},
    pool_warn_threshold=0.8,
)
```

The values above are illustrative. Choose the request limit using the number of
connections used per request and capacity reserved for other work. This limit
belongs to one middleware instance in one process; each server worker has its
own limiter and engine. It is not a cluster-wide connection limit.

When no permit becomes available within `request_queue_timeout`, the middleware
returns HTTP **503** with **Retry-After: 1**, without calling the route. It does
not retry requests or transactions. Set `max_concurrent_requests=None` (the
default) to disable admission control. Queue time must be finite and positive.
Waiting requests can still accumulate during that deadline; this is not a hard
limit on queue length or memory usage.

All non-excluded HTTP paths count towards the limit, even if they do not query
the database. `exclude_paths` uses exact ASGI path matching, bypasses admission
control and creates no request session. Exclusion does not provide database
health checking: use a separate readiness engine, and keep liveness independent
of the database. See [Health Checks](health-checks.md).

The permit is released when the final body is sent, or when request handling
fails or is cancelled. Streaming responses hold a permit until their body ends.
Background tasks are outside the HTTP limit and need their own concurrency
controls if they perform substantial database work.

`db(multi_sessions=True, max_concurrent=N)` remains a separate limit on work
within one explicit context. It does not limit other requests. If a request
opens several database sessions, a request limit alone cannot guarantee that
the pool will never fill. Checkout deadlines and service-level batching are
still useful.

## Request sessions end before background tasks

For a normal response, the middleware finalizes the request transaction when
the complete response body is ready, before sending the buffered response and
before Starlette runs background tasks. This returns the connection to the pool
even when a background task is slow. A commit failure still prevents a successful
response from being sent.

Background tasks must create their own session and transaction:

```python
async def record_audit(item_id: int):
    async with db(commit_on_exit=True):
        db.session.add(AuditRecord(item_id=item_id))


@app.post("/items")
async def create_item(background_tasks: BackgroundTasks):
    item = Item()
    db.session.add(item)
    await db.session.flush()
    background_tasks.add_task(record_audit, item.id)
    return {"id": item.id}
```

This example assumes middleware `commit_on_exit=True`. Pass scalar identifiers
to background tasks rather than request sessions or ORM objects attached to them.
Accessing the finalized request session through `db.session` raises an error
explaining that an explicit `async with db()` is required.

!!! warning "`@app.middleware("http")` below this middleware breaks the ordering"
    `BaseHTTPMiddleware` runs the application in a child task and hands
    response messages to a queue, so that task reaches `self.background()` as
    soon as the message is queued — while the outer middleware is still
    awaiting the commit. Background tasks then run *concurrently* with
    finalization, and a task that deletes the file a `FileResponse` just served
    can win the race. This affects any outer middleware, not just this one; the
    only way to rule it out is to write that middleware as pure ASGI instead of
    `@app.middleware("http")`. A failing commit still prevents a successful
    response either way, because the response start stays buffered.

**Migration:** background code that previously relied on the request session
must open its own context. Errors after the response has been sent cannot roll
back the committed request transaction or change its HTTP status. A failed
background transaction rolls back only its own work. Do not move essential
atomic request writes into a background task.

## `yield` dependencies that own a transaction

FastAPI runs `yield` dependency teardown *after* the response is sent — the same
point at which the request session is finalized. A dependency that owns its
transaction explicitly is therefore **exempt** from the early finalization:

```python
async def transaction():
    async with db.session.begin():
        yield


@app.post("/entries", dependencies=[Depends(transaction)])
async def create_entry():
    await db.session.execute(insert(Entry).values(value=1))
    return {"ok": True}
```

Here the dependency, not the middleware, decides when the transaction commits,
so the session stays open until the teardown finishes and its connection is
returned then. The same applies to a `begin_nested()` savepoint held across the
response.

The exemption is deliberately narrow — it covers only transactions your code
began with `db.session.begin()` or `db.session.begin_nested()`. A transaction
SQLAlchemy autobegan on the first `db.session.execute(...)` has no such owner
and is still finalized early, which is what keeps a slow background task from
pinning the request connection.

!!! warning "A transaction-owning dependency holds its connection longer"
    For the duration of the teardown the connection is still checked out, so a
    dependency of this shape and a slow background task on the same route give
    up the early release. Under [pool pressure](health-checks.md), prefer
    `commit_on_exit=True` plus autobegin, and keep explicit transactions for
    routes that genuinely need to control the commit boundary.

**Migration:** no change is needed for `yield` dependencies that wrap
`db.session.begin()` — they behave as they did before the early finalization was
introduced. A dependency whose teardown touches `db.session` *without* owning a
transaction will see the "closed after response finalization" error and must
open its own `async with db()` context.
