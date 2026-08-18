"""FastAPI dependency providers.

The Mongo/Redis clients are created once in the app's lifespan handler and
stashed on `app.state` (see `app/main.py`); these providers just hand out
references to those singletons. Routing business logic through `Depends()`
instead of module-level globals is what lets tests substitute fakeredis /
mongomock instances by overriding `app.dependency_overrides`, without any
route or service code needing to know the difference.
"""
from fastapi import Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from redis.asyncio import Redis


def get_db(request: Request) -> AsyncIOMotorDatabase:
    return request.app.state.db


def get_redis(request: Request) -> Redis:
    return request.app.state.redis
