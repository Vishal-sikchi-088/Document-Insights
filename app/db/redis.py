"""Redis client lifecycle.

Like the Mongo client, redis-py's asyncio client owns an internal
connection pool, so it's created once at startup and shared as a
singleton via `app.state` rather than opened per request.
"""
import logging

from redis.asyncio import Redis
from redis.asyncio import from_url as redis_from_url

logger = logging.getLogger(__name__)


def create_redis_client(url: str) -> Redis:
    return redis_from_url(url, decode_responses=True)


async def ping(client: Redis) -> bool:
    """Report whether Redis is reachable, for the /health endpoint.

    Same reasoning as `mongo.ping`: a health check must never raise, so a
    connection error here is logged and converted into an `unhealthy`
    signal rather than propagating as a 500.
    """
    try:
        return bool(await client.ping())
    except Exception:
        logger.exception("redis health check failed")
        return False
