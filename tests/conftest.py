"""Shared pytest fixtures.

Everything here runs against in-memory fakes (mongomock-motor, fakeredis)
rather than real MongoDB/Redis — no external services need to be running
for `pytest` to work, and the fakes are fast enough that even the
integration tests, which exercise the full FastAPI stack end to end,
complete in a fraction of a second each. See the README for the trade-off
this implies (index enforcement, some Mongo aggregation edge cases aren't
covered by mongomock) and where real-service integration tests would fit.
"""
import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient
from mongomock_motor import AsyncMongoMockClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.dependencies import get_db, get_redis
from app.main import app
from app.repositories.document_repository import DocumentRepository
from app.services.cache_service import CacheService
from app.services.document_service import DocumentService
from app.services.queue_service import QueueService
from app.services.rate_limiter import RateLimiterService

TEST_QUEUE_KEY = "test:document_insights:queue"
TEST_MAX_ACTIVE_JOBS = 3
TEST_CACHE_TTL_SECONDS = 3600
TEST_LOCK_TTL_SECONDS = 120


class BrokenRedis:
    """Stand-in for a completely unreachable Redis client.

    Every operation raises the same error a real client raises on a
    connection failure, so the graceful-degradation branches in
    RateLimiterService/CacheService can be exercised deterministically
    instead of needing to actually take a Redis instance down mid-test.
    """

    def register_script(self, script: str):
        async def _raise(*args, **kwargs):
            raise RedisConnectionError("simulated redis outage")

        return _raise

    def pipeline(self, *args, **kwargs):
        raise RedisConnectionError("simulated redis outage")

    async def _raise(self, *args, **kwargs):
        raise RedisConnectionError("simulated redis outage")

    def __getattr__(self, name):
        return self._raise


@pytest.fixture
def broken_redis_client():
    return BrokenRedis()


@pytest.fixture
def mongo_db():
    # A fresh mock client per test, so tests never depend on execution
    # order or leak state into one another.
    return AsyncMongoMockClient()["test_db"]


@pytest.fixture
def redis_client():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def document_repository(mongo_db) -> DocumentRepository:
    return DocumentRepository(mongo_db)


@pytest.fixture
def rate_limiter(redis_client) -> RateLimiterService:
    return RateLimiterService(
        redis_client, max_active_jobs=TEST_MAX_ACTIVE_JOBS, entry_ttl_seconds=TEST_LOCK_TTL_SECONDS
    )


@pytest.fixture
def cache_service(redis_client) -> CacheService:
    return CacheService(
        redis_client, cache_ttl_seconds=TEST_CACHE_TTL_SECONDS, lock_ttl_seconds=TEST_LOCK_TTL_SECONDS
    )


@pytest.fixture
def queue_service(redis_client) -> QueueService:
    return QueueService(redis_client, queue_key=TEST_QUEUE_KEY)


@pytest.fixture
def document_service(document_repository, rate_limiter, cache_service, queue_service) -> DocumentService:
    return DocumentService(
        repository=document_repository, rate_limiter=rate_limiter, cache=cache_service, queue=queue_service
    )


@pytest.fixture
async def api_client(mongo_db, redis_client):
    """An httpx client wired against the real FastAPI app, with only the
    Mongo/Redis clients swapped for in-memory fakes via dependency
    overrides. Every layer above that — routers, services, repository —
    runs unmodified, so these tests exercise the real request path rather
    than a reimplementation of it.
    """
    app.dependency_overrides[get_db] = lambda: mongo_db
    app.dependency_overrides[get_redis] = lambda: redis_client
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
