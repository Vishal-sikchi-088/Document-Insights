"""FastAPI dependency providers.

The Mongo/Redis clients are created once in the app's lifespan handler and
stashed on `app.state` (see `app/main.py`); these providers just hand out
references to those singletons and wire the layers built on top of them.
Routing everything through `Depends()` instead of module-level globals is
what lets tests substitute fakeredis/mongomock instances by overriding
`app.dependency_overrides`, without any route or service code needing to
know the difference.
"""
from fastapi import Depends, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.core.ttl import compute_worst_case_job_lifetime_seconds
from app.repositories.document_repository import DocumentRepository
from app.services.cache_service import CacheService
from app.services.document_service import DocumentService
from app.services.queue_service import QueueService
from app.services.rate_limiter import RateLimiterService


def get_db(request: Request) -> AsyncIOMotorDatabase:
    return request.app.state.db


def get_redis(request: Request) -> Redis:
    return request.app.state.redis


def get_document_repository(db: AsyncIOMotorDatabase = Depends(get_db)) -> DocumentRepository:
    return DocumentRepository(db)


def get_rate_limiter(
    redis_client: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> RateLimiterService:
    return RateLimiterService(
        redis_client,
        max_active_jobs=settings.rate_limit_max_active_jobs,
        entry_ttl_seconds=compute_worst_case_job_lifetime_seconds(settings),
    )


def get_cache_service(
    redis_client: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> CacheService:
    return CacheService(
        redis_client,
        cache_ttl_seconds=settings.cache_ttl_seconds,
        lock_ttl_seconds=compute_worst_case_job_lifetime_seconds(settings),
    )


def get_queue_service(
    redis_client: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> QueueService:
    return QueueService(redis_client, queue_key=settings.redis_queue_key)


def get_document_service(
    repository: DocumentRepository = Depends(get_document_repository),
    rate_limiter: RateLimiterService = Depends(get_rate_limiter),
    cache: CacheService = Depends(get_cache_service),
    queue: QueueService = Depends(get_queue_service),
) -> DocumentService:
    return DocumentService(repository=repository, rate_limiter=rate_limiter, cache=cache, queue=queue)
