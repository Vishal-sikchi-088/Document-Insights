"""Mongo client lifecycle and index bootstrap.

`AsyncIOMotorClient` already maintains its own connection pool (default
maxPoolSize=100) and is safe to share across requests, so the app creates
exactly one at startup (see `app/main.py`) and hands that same instance to
every request via `app.state` — there's no per-request connect/disconnect.
"""
import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

logger = logging.getLogger(__name__)

DOCUMENTS_COLLECTION = "documents"


def create_mongo_client(uri: str) -> AsyncIOMotorClient:
    return AsyncIOMotorClient(uri, uuidRepresentation="standard")


async def create_indexes(db: AsyncIOMotorDatabase) -> None:
    """Ensure the indexes the API relies on for correctness and query cost.

    Both back a real access pattern rather than a speculative one:
    - (user_id, status) is the exact shape of "list a user's documents,
      optionally filtered by status" — the busiest read path in the API.
    - content_hash backs the cache-hit lookup used by the submission flow.

    `create_index` is idempotent, so running this on every startup is safe
    and means there's no separate migration step to remember to run.
    """
    documents = db.get_collection(DOCUMENTS_COLLECTION)
    await documents.create_index([("user_id", 1), ("status", 1)], name="user_status_idx")
    await documents.create_index([("content_hash", 1)], name="content_hash_idx")
    logger.info("mongo indexes ensured", extra={"collection": DOCUMENTS_COLLECTION})


async def ping(db: AsyncIOMotorDatabase) -> bool:
    """Report whether Mongo is reachable, for the /health endpoint.

    Broad exception handling is deliberate here, not a swallowed error: a
    health check's contract is "never raise, always answer," so any driver
    exception (timeout, auth failure, network error) is logged with its
    traceback and converted into an `unhealthy` signal instead of a 500.
    """
    try:
        await db.command("ping")
        return True
    except Exception:
        logger.exception("mongo health check failed")
        return False
