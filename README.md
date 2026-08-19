# Document Insights API

This is a backend service. You send it a document (some text), and it processes that document
in the background and gives you back a short summary of it.

There's no real AI summarization here — the assignment says that's not needed. Instead, the
"summary" is generated with simple logic (first few sentences + word count). What actually
matters for this project is everything *around* that: the API design, how data is stored, how
background jobs are handled, caching, and making the whole thing production-ready.

## Quick start

```bash
docker compose up --build
```

That one command starts everything: the API, the background worker, MongoDB, and Redis. You
don't need to create any config file first — every setting already has a sensible default.

Try it out:

```bash
# submit a document
curl -X POST http://localhost:8000/documents \
  -H "Content-Type: application/json" \
  -d '{"user_id": "alice", "title": "Q3 Notes", "content": "Revenue grew 12% quarter over quarter. Churn held steady. Headcount plan needs revisiting."}'
# -> {"document_id": "...", "status": "queued"}

# check on it later (it takes 10-30 seconds to "process")
curl http://localhost:8000/documents/<document_id>

# see all of a user's documents
curl "http://localhost:8000/users/alice/documents?page=1&page_size=20"
curl "http://localhost:8000/users/alice/documents?status=completed"

# is the service healthy?
curl http://localhost:8000/health
```

There's also a browser-based interactive docs page at `http://localhost:8000/docs` where you can
try every endpoint by clicking buttons instead of using curl.

There's also a small standalone testing page at `tools/api_tester.html` — just open that file
in your browser (no server needed for the page itself) for a friendlier way to try every
endpoint, watch documents move from `queued` to `completed` live, and trigger the rate-limit /
caching behavior with one click.

## Running it without Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# start just MongoDB and Redis in Docker
docker compose up -d mongo redis

# terminal 1: the API
uvicorn app.main:app --reload

# terminal 2: the background worker
python -m app.workers.summarizer_worker
```

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest
```

There are 72 tests, and none of them need a real MongoDB or Redis running — they use fast,
in-memory stand-ins instead. That means `pytest` works right away, no setup required.

(One small detail: the fake Redis needs an extra package called `lupa` to run a small script
the rate limiter uses. It's already listed in `requirements-dev.txt`, so `pip install` handles
it automatically. It's only needed for tests, not for actually running the app.)

- `tests/unit/` — tests each piece on its own (the database layer, each service, the worker)
- `tests/integration/` — tests the whole API from the outside, like a real client would

## How the code is organized

The code is split into layers, and each layer only knows about the one below it:

**Routes (HTTP) → Services (business logic) → Repository (database)**

A route never talks to the database directly. A service never knows anything about HTTP. Only
one file (`document_repository.py`) is allowed to write MongoDB queries.

```
app/
├── main.py                  starts the app, connects to Mongo/Redis, wires everything together
├── config.py                all the settings, with defaults, read from environment variables
├── logging_config.py        makes logs print as JSON, easier to search through
├── db/                      creates the Mongo and Redis connections, plus health checks
├── models/                  the shapes of data: what a request looks like, what's stored, etc.
├── repositories/            the only place that talks to MongoDB directly
├── services/
│   ├── document_service.py  the main flow: check cache → check rate limit → save → queue it
│   ├── rate_limiter.py      keeps track of how many active jobs each user has
│   ├── cache_service.py     the "did we already summarize this exact text?" cache
│   ├── queue_service.py     the job queue the worker reads from
│   └── summarization.py     generates the (fake) summary
├── routers/                 the actual API endpoints — thin, just translates HTTP <-> services
├── workers/                 the background worker that does the actual "processing"
└── core/                    shared helpers: error handling, dependency wiring, etc.
```

The background worker runs as its **own separate process** (its own container, in fact), not
as part of the API. This means the API stays fast and responsive even if a lot of documents are
queued up, and if you need more processing power, you just run more worker copies — no extra
coordination needed, because Redis and MongoDB already guarantee two workers can't grab the same
job (more on that below).

## How data is stored (MongoDB)

Everything lives in one collection, `documents`:

| Field | What it is |
|---|---|
| `_id` | the document's unique ID |
| `user_id`, `title`, `content` | what the user submitted |
| `content_hash` | a fingerprint of `content`, used to detect duplicate text |
| `status` | `queued` → `processing` → `completed` or `failed` |
| `summary` | the generated summary, once done |
| `error` | why it failed, if it failed |
| `attempts` | how many times processing was tried |
| `cached` | true if this result was reused from an earlier identical submission |
| `created_at`, `updated_at` | timestamps |

Two indexes are created automatically every time the app starts:

- **`user_id` + `status`** — makes "show me this user's documents, optionally filtered by
  status" fast. This is the most common lookup in the whole API.
- **`content_hash`** — makes it fast to check "have we seen this exact text before?"

## How Redis is used

Redis is used for three different jobs, kept deliberately separate from each other:

| What it's for | How it works | If Redis goes down |
|---|---|---|
| Rate limiting | Tracks each user's currently-active documents | Still lets requests through (see below) |
| Content cache | Stores `text → summary` so identical text isn't reprocessed | Just treated as "not cached," so it reprocesses |
| Duplicate-submission handling | Coordinates two people submitting the same new text at once | Falls back to just processing it normally |
| Job queue | The actual list of documents waiting to be processed | This one **can't** fail silently — see below |

**Why rate limiting "fails open":** if Redis is unreachable, we'd rather let a request through
(and log a warning) than reject every single submission just because a safety feature is
temporarily down. Rate limiting protects the system; it isn't a core guarantee, so it's okay for
it to step aside if it can't do its job.

**Why the job queue is different:** the queue *is* the actual mechanism that gets documents
processed. If Redis is down, there's no safe way to "pretend" a document is queued — it would
just sit there forever with no worker ever picking it up. So this one fails loudly instead of
quietly accepting a document it can't actually deliver.

**Why a "set" instead of a simple counter for rate limiting:** a counter only stays accurate if
every +1 is matched by exactly one -1. A set is more forgiving — removing something that was
never added, or removing it twice, doesn't break anything. That mattered here because Redis
failures could otherwise leave a counter permanently wrong.

## Handling things happening at the same time (race conditions)

- **Two workers grabbing the same job.** The queue only ever hands a given job to one worker.
  As a backup, before a worker starts processing, it does one atomic "claim" update in MongoDB
  (only succeeds if the document is still `queued`). If two workers somehow got the same job,
  only one of these claims would succeed — the other one just backs off.
- **Two requests hitting the rate limit at the exact same moment.** Checking "is this user under
  the limit?" and then adding them both takes two separate steps — which is exactly the kind of
  gap where two requests could sneak through together. To close that gap, the check-and-add
  happens as one indivisible operation in Redis (a small script), not two separate steps.
- **Two people submitting the exact same new text at the same moment.** The cache only helps
  with text that's *already* been summarized — it can't catch two brand-new identical
  submissions arriving a few milliseconds apart. So instead: whichever request arrives first
  becomes the one that actually gets processed, and the second one just waits and gets the same
  result once the first one finishes — instead of the system doing the same work twice. If the
  first one fails, anyone still waiting gets processed independently instead of waiting forever.
- **A rare edge case on top of that.** There's a tiny window where the "first" request could
  still end up doing unnecessary work, if some *other* request finishes and caches the same text
  in between two of its steps. There's an extra check in place specifically for this, and it's
  covered by its own test.

## Decisions I made, and why

The assignment says to make reasonable assumptions where things are unclear, and to write them
down. Here they are:

- **Submitting text that's already cached still creates a new document entry**, immediately
  marked `completed`. It doesn't count toward your rate limit, since nothing is actually
  running — you're just getting an instant answer.
- **The cache isn't tied to a specific user.** If two different people submit identical text,
  they both benefit from the same cached summary. A summary only depends on the text, not on who
  sent it, so there's no reason to make two people wait for the same work twice.
- **A bad document ID and a document ID that just doesn't exist both return the same "not
  found" response**, rather than treating them differently. A client only needs to know "does
  this work or not" — and not explaining exactly *why* an ID is invalid is also slightly safer.
- **Document IDs are generated before anything is saved.** This lets the rate limit get checked
  first — so a rejected request costs almost nothing, instead of writing to the database and
  then having to undo it.
- **A failed job's retry-wait doesn't block the worker from doing anything else.** It waits in
  the background instead, so one slow retry doesn't hold up every other document behind it.
- **Unexpected fields in a request are rejected**, not silently ignored. If someone sends a typo
  like `usre_id` by mistake, they get a clear error instead of the field just being dropped
  with no explanation.
- **No CORS / browser security settings**, since this API isn't meant to be called from a
  website — except a small dev-only allowance so the local testing page
  (`tools/api_tester.html`) can talk to it. That allowance only turns on in development mode.
- **Simple page-number pagination** (`page`, `page_size`) rather than something more advanced,
  since that's what the assignment asked for and it's easier for a client to use. The trade-off
  is explained in "What I'd do differently" below.

## What I'd improve with more time

- **Recovering from a worker crashing mid-job.** Right now, if a worker process dies while a
  document is `processing` (not a normal simulated failure — an actual crash), that document
  just stays stuck. A background check that notices "this has been processing for way too long"
  and retries it would fix that.
- **A better pagination style for very large result sets.** The current approach is simple and
  matches what the assignment asked for, but gets slower the deeper you page into a very large
  list. A more scalable approach exists, just adds complexity that isn't needed at this size.
- **Real authentication.** Right now, anyone can claim to be any `user_id` — there's no login,
  no verification. Fine for this exercise, but the first thing I'd add before this touched real
  user accounts.
- **Better traceability across logs.** Right now you can see logs from the API and the worker
  separately, but there's no shared "request ID" tying one document's full journey together
  across both.
- **Metrics/monitoring.** Right now you can only see what happened by reading logs. Dashboards
  showing things like "how many jobs failed in the last hour" would be a natural next step.
- **A smarter recovery when duplicate work fails.** Right now, if the "first" request for some
  new text fails, everyone else waiting on it just gets their own independent shot at
  processing it — simple and safe, but it does mean the caching benefit is lost for that group
  if it happens a lot.

## Settings

Every setting has a working default — see `.env.example` for the full list with explanations.
The ones most relevant to the assignment:

| Setting | Default | What it controls |
|---|---|---|
| `RATE_LIMIT_MAX_ACTIVE_JOBS` | `3` | max documents a user can have in progress at once |
| `CACHE_TTL_SECONDS` | `86400` (24h) | how long a cached summary stays usable |
| `WORKER_MIN/MAX_PROCESSING_SECONDS` | `10` / `30` | how long fake "processing" takes |
| `WORKER_FAILURE_RATE` | `0.1` | chance a job randomly fails, to test error handling |
| `WORKER_MAX_RETRIES` | `2` | how many times a failed job retries before giving up |
