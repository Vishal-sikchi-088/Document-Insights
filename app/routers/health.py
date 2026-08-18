"""Liveness/readiness probe.

Deliberately bypasses the service/repository layers: its only job is to
answer "are the things this process depends on reachable," so it talks to
the Mongo/Redis clients directly rather than through business-logic
services that don't exist for this purpose.
"""
from fastapi import APIRouter, Depends, Response, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from redis.asyncio import Redis

from app.core.dependencies import get_db, get_redis
from app.db.mongo import ping as ping_mongo
from app.db.redis import ping as ping_redis

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check(
    response: Response,
    db: AsyncIOMotorDatabase = Depends(get_db),
    cache: Redis = Depends(get_redis),
) -> dict:
    mongo_healthy = await ping_mongo(db)
    redis_healthy = await ping_redis(cache)
    all_healthy = mongo_healthy and redis_healthy

    # A degraded dependency isn't a client error, so this isn't a 4xx; it's
    # a 503 telling load balancers / orchestrators to stop routing traffic
    # here until the dependency recovers.
    if not all_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if all_healthy else "degraded",
        "dependencies": {
            "mongodb": "ok" if mongo_healthy else "unreachable",
            "redis": "ok" if redis_healthy else "unreachable",
        },
    }
