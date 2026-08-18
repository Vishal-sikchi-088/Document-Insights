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


def _worst_case_job_lifetime_seconds(settings: Settings) -> int:
    """Upper bound on how long a single document might stay in-flight
    across every retry attempt (processing time plus doubling backoff).

    Used as the TTL safety net for the rate-limit entry and the cache
    processing lock: long enough that it never expires out from under a
    job that's still legitimately running, short enough to self-heal
    promptly if a worker crashes without releasing either one.
    """
    attempts = settings.worker_max_retries + 1
    total_processing_time = attempts * settings.worker_max_processing_seconds
    total_backoff_time = sum(
        settings.worker_retry_backoff_seconds * (2**attempt) for attempt in range(settings.worker_max_retries)
    )
    safety_buffer_seconds = 60
    return total_processing_time + total_backoff_time + safety_buffer_seconds


def get_rate_limiter(
    redis_client: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> RateLimiterService:
    return RateLimiterService(
        redis_client,
        max_active_jobs=settings.rate_limit_max_active_jobs,
        entry_ttl_seconds=_worst_case_job_lifetime_seconds(settings),
    )


def get_cache_service(
    redis_client: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> CacheService:
    return CacheService(
        redis_client,
        cache_ttl_seconds=settings.cache_ttl_seconds,
        lock_ttl_seconds=_worst_case_job_lifetime_seconds(settings),
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
