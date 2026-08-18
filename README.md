# Document Insights API

A backend service that accepts document text, processes it asynchronously (simulated AI
summarization), and returns structured summaries. Built with FastAPI, MongoDB, and Redis.

No real AI/LLM integration — per the assignment, that's out of scope. The focus here is API
design, data modeling, async processing, caching, and production readiness.

## Quick start

```bash
docker compose up --build
```

That's it — the API, worker, MongoDB, and Redis all start together. No `.env` file is required;
every setting has a built-in default (see `app/config.py`), and `docker-compose.yml` only
overrides the two values that have to change inside Compose's network (`MONGODB_URI`,
`REDIS_URL` — services reach each other by name, not `localhost`).

```bash
# submit a document
curl -X POST http://localhost:8000/documents \
  -H "Content-Type: application/json" \
  -d '{"user_id": "alice", "title": "Q3 Notes", "content": "Revenue grew 12% quarter over quarter. Churn held steady. Headcount plan needs revisiting."}'
# -> {"document_id": "...", "status": "queued"}

# poll for the result (processing takes 10-30s)
curl http://localhost:8000/documents/<document_id>

# list a user's documents
curl "http://localhost:8000/users/alice/documents?page=1&page_size=20"
curl "http://localhost:8000/users/alice/documents?status=completed"

# liveness/readiness
curl http://localhost:8000/health
```

Interactive API docs (Swagger UI, generated from the Pydantic models) are at
`http://localhost:8000/docs`.

## Local development (without Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# bring up just the datastores
docker compose up -d mongo redis

# terminal 1
uvicorn app.main:app --reload

# terminal 2
python -m app.workers.summarizer_worker
```

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest
```

72 tests, unit and integration, all running against in-memory fakes
(`mongomock-motor`, `fakeredis`) — no live MongoDB/Redis required. `fakeredis` needs the optional
`lupa` package to actually execute Lua (used by the rate limiter's atomic script); it's pinned in
`requirements-dev.txt`. Without it, those specific assertions would still pass, but only because
the rate limiter's Redis-unavailable fail-open path would silently swallow the script call —
worth knowing if `lupa` fails to build in your environment (it's dev-only; nothing at runtime
depends on it).

- `tests/unit/` — repository, services (rate limiter, cache, queue, document orchestration),
  the worker, and model validation, each tested in isolation
- `tests/integration/` — the real FastAPI app end to end via `httpx.AsyncClient`, only the
  Mongo/Redis clients swapped for fakes via `app.dependency_overrides`

## Architecture

Layered, with a clear one-way dependency direction: **routers → services → repository**. Routers
never touch Mongo/Redis directly; services never know about HTTP; the repository is the only
module that speaks Mongo query syntax.

```
app/
├── main.py                    composition root: builds the app, wires Mongo/Redis into
│                               app.state on startup, registers routers + exception handlers
├── config.py                  pydantic-settings; every field has a default matching .env.example
├── logging_config.py          structured JSON log formatter (stdlib logging, no extra dependency)
├── db/
│   ├── mongo.py                client factory, index bootstrap, /health ping
│   └── redis.py                client factory, /health ping
├── models/
│   ├── document.py             request/response DTOs + the internal Mongo-shaped model
│   └── enums.py                DocumentStatus
├── repositories/
│   └── document_repository.py  the only module that queries Mongo
├── services/
│   ├── document_service.py     orchestrates submission: cache → rate limit → persist → enqueue
│   ├── rate_limiter.py         per-user concurrent-job limit (Redis)
│   ├── cache_service.py        content-hash summary cache + duplicate-submission coordination
│   ├── queue_service.py        Redis-list job queue
│   └── summarization.py        pure mock-summary generator
├── routers/                    documents.py, users.py, health.py — thin HTTP translation only
├── workers/
│   └── summarizer_worker.py    standalone process: claims jobs, drives them to a terminal state
└── core/
    ├── dependencies.py         FastAPI DI graph
    ├── exceptions.py           domain exceptions
    ├── error_handlers.py       the one place exceptions become HTTP responses
    ├── hashing.py               content hashing (sha256)
    ├── redis_keys.py            centralized Redis key naming
    └── ttl.py                   shared TTL math (API and worker both use this)
```

The worker runs as its **own process** (its own container in Compose), not an in-process asyncio
task inside the API. That's a deliberate choice: it means the API staying responsive is
decoupled from processing throughput, and horizontal scaling is just running more worker
replicas — safe without any coordination beyond what Redis (`BLPOP`'s atomic delivery) and Mongo
(an atomic status transition) already guarantee. See "Concurrency & race conditions" below.

## MongoDB schema

Single `documents` collection:

| Field | Type | Notes |
|---|---|---|
| `_id` | ObjectId | Generated **client-side** (see "Design decisions" below), not by Mongo |
| `user_id` | string | |
| `title`, `content` | string | |
| `content_hash` | string | sha256 of `content`, used for cache lookups |
| `status` | string | `queued` \| `processing` \| `completed` \| `failed` |
| `summary` | object \| null | `{summary_text, word_count, character_count, key_points}` |
| `error` | string \| null | set only on terminal failure |
| `attempts` | int | incremented atomically on each processing claim |
| `cached` | bool | true if this document's result came from the cache/a leader, not its own processing |
| `created_at`, `updated_at` | datetime | timezone-aware UTC |

Indexes (created idempotently on every startup — see `create_indexes`, no separate migration
step to remember to run):

- **`{user_id: 1, status: 1}`** — backs the busiest read path, `GET /users/{id}/documents`
  with an optional status filter.
- **`{content_hash: 1}`** — backs cache-related lookups against the collection.

## Redis usage

Three distinct concerns, deliberately kept apart with different key prefixes and different
failure semantics:

| Concern | Structure | TTL | On Redis outage |
|---|---|---|---|
| Rate limiting | Set of active `document_id`s per user | worst-case job lifetime (safety net; `release()` is the real mechanism) | **fails open** — logged, submission allowed. Rate limiting is a protective measure, not a correctness guarantee; rejecting every request because a non-critical dependency is down would be worse than the risk it's protecting against. |
| Content cache | `content_hash → summary JSON` | `CACHE_TTL_SECONDS` (business freshness policy) | **fails open** — treated as a miss, document processes normally, just slower |
| Duplicate-submission lock + waiters | `SET NX` lock, `RPUSH` waiter list, both per content hash | worst-case job lifetime | **fails closed** — an unreachable Redis is never trusted to mean "you're the leader"; the caller just processes independently |
| Job queue | `RPUSH`/`BLPOP` list | n/a | **not graceful** — this *is* the processing mechanism; an unreachable queue means no work ever gets done, so it fails loudly rather than silently accepting a document that'll never be picked up |

A plain integer counter for rate limiting was considered and rejected: a counter only stays
correct if every increment is matched by exactly one decrement, and a set is idempotent by
construction — releasing a slot twice, or one that was never acquired (the fail-open path),
can't corrupt the count the way a mismatched counter could.

## Concurrency & race conditions

- **Two workers claiming the same job.** `BLPOP` delivers each queued id to exactly one worker
  process. As a second, independent guard for the edge case where an id is somehow delivered
  twice (a manual requeue racing a redelivery), `DocumentRepository.claim_for_processing` does
  an atomic `find_one_and_update({_id, status: "queued"}, {$set: {status: "processing"}})` —
  only the first caller's update matches; the second gets `None` back and no-ops. No distributed
  lock needed; Mongo's atomic update *is* the lock.
- **Rate-limit check-then-increment.** A plain `SCARD` followed by `SADD` leaves a window where
  concurrent requests can all read a count under the limit before any of them writes. The check
  and the mutation are wrapped in a single Lua script instead, which Redis executes atomically.
- **Two submissions of identical new content racing in.** The content-hash cache only catches
  content some *earlier* request already finished — it can't catch two requests arriving with
  the same brand-new content microseconds apart. A `SET NX` lock elects one request as "leader"
  (it actually enqueues a job); the other registers as a "waiter" instead of triggering its own
  redundant 10–30s run. The worker resolves every waiter directly from the leader's result once
  it's known. If the leader's job ultimately fails after exhausting retries, its waiters are
  promoted onto the real queue as independent jobs rather than left stranded.
- **The leader-election race's own race.** Even the leader path has a narrow window: another
  request could finish and cache the same content in the gap between this request's initial
  cache miss and it winning the lock. Double-checked locking closes it — after winning the lock,
  the leader re-checks the cache once before committing to enqueue; a hit there resolves it
  immediately instead of doing redundant work "as leader." This is covered by a dedicated
  regression test (`test_leader_resolves_via_cache_if_populated_between_miss_check_and_lock_win`).

## Design decisions & assumptions

The assignment explicitly invites reasonable assumptions where the spec is ambiguous. Here's
what I assumed and why, beyond what's already covered above:

- **A cache hit at submission time still creates a Mongo document** — immediately `completed`,
  flagged `cached: true` — rather than returning a summary with no document record at all. This
  keeps `GET /documents/{id}` and the user's document list consistent regardless of how a
  document reached its terminal state. It does **not** count against the rate limit, since
  nothing is actually "in flight" from the caller's perspective.
- **The cache is keyed globally by content hash, not scoped per user.** A summary is a pure
  function of content — two different users submitting identical text should get the same
  cached result. The spec's phrasing ("if *a user* submits...") reads user-centric, but scoping
  the cache per-user would mean two different users paying for redundant processing of literally
  identical content, which seems like the less useful reading.
- **A malformed document id and a well-formed-but-absent one both return `404`**, not a `400`
  vs. `404` split. A client only ever needs to know "does this id resolve"; not distinguishing
  the two also avoids leaking id-format details to a caller.
- **The document id is generated client-side** (`bson.ObjectId()`, no Mongo round-trip) before
  any write happens, specifically so the rate limit can be checked and reserved *before* the
  document is persisted — a rejected submission should cost one Redis call, not a Mongo insert
  that then has to be reasoned about as "rejected but written."
- **Retry backoff doesn't block the worker's main loop.** A failed job's backoff sleep runs as a
  tracked background `asyncio` task that re-enqueues the job when it elapses, rather than
  `await asyncio.sleep()`-ing inline — otherwise one document's retry delay would stall every
  other queued document behind it in that worker process.
- **`extra="forbid"` on the submission payload.** Pydantic ignores unknown fields by default;
  forbidding them turns a client's typo'd or misunderstood field name into an explicit `422`
  instead of a request that looks accepted but silently did less than the caller expected.
- **No CORS or other browser-facing security headers.** This API has no browser client in scope
  for this exercise — a permissive CORS policy would be unused attack surface, and a restrictive
  one would need an origin list nobody has specified. Worth revisiting the moment a real frontend
  origin exists.
- **Offset-based pagination** (`page`/`page_size`, per the spec) over cursor/keyset pagination.
  Simpler and matches the spec's literal parameters, at the cost of `skip()` getting more
  expensive on very deep pages against a large collection — see "What I'd do differently."

## What I'd do differently with more time

- **Stale `processing` document reconciliation.** If a worker process crashes mid-job (not a
  simulated failure — an actual crash), that document is left in `processing` forever with no
  automatic recovery; the Redis-side rate-limit/lock entries self-heal via TTL, but the Mongo
  document doesn't. A periodic sweep (worker startup, or a scheduled job) that requeues
  documents stuck in `processing` past some age threshold would close this gap. The
  unexpected-exception branch in the worker's job loop — which currently just logs and moves
  on, leaving the document exactly where it was — points at the same gap.
- **Cursor/keyset pagination** for `GET /users/{id}/documents` once a user's document count gets
  large enough that skip-based pagination's linear scan cost becomes noticeable. Went with
  offset-based pagination here since it matches the spec's literal `page`/`page_size` parameters
  and is simpler for a client to reason about at this scale.
- **No authentication/authorization.** `user_id` is taken as given, with no verification that
  the caller is who they claim to be. Out of scope for this exercise, but the first thing to add
  before this touched real user data.
- **Correlation/request IDs** threaded through the structured logs, so a single submission's
  full lifecycle (API request → enqueue → worker pickup → completion) could be traced across
  both processes' log streams by one id.
- **Metrics/tracing** (Prometheus counters for queue depth and job outcomes, OpenTelemetry spans
  across the API → Redis → worker → Mongo path) — the JSON logs give you grep-ability, not
  aggregate visibility into throughput or failure rate over time.
- **Waiter promotion on leader failure gives every stranded follower an independent shot**
  rather than re-electing a new leader among them. Simple and correct, but under high duplicate-
  submission volume it forfeits the dedup optimization for that batch — worth revisiting if
  duplicate-content bursts turn out to be common in practice.

## Environment variables

See `.env.example` for the full list with inline documentation — every variable has a sane
default, so no `.env` file is required to run the stack. The ones most relevant to the
assignment's core requirements:

| Variable | Default | Purpose |
|---|---|---|
| `RATE_LIMIT_MAX_ACTIVE_JOBS` | `3` | Max concurrent `queued`/`processing` documents per user |
| `CACHE_TTL_SECONDS` | `86400` | How long a cached summary stays valid |
| `WORKER_MIN/MAX_PROCESSING_SECONDS` | `10` / `30` | Simulated processing time range |
| `WORKER_FAILURE_RATE` | `0.1` | Probability a job randomly fails |
| `WORKER_MAX_RETRIES` | `2` | Retries after the first attempt before terminal `failed` |
