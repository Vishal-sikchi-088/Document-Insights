from app.models.document import SummaryModel
from app.services.cache_service import CacheService

_SUMMARY = SummaryModel(summary_text="s", word_count=1, character_count=1, key_points=["s"])


class TestCacheService:
    async def test_get_returns_none_on_miss(self, cache_service):
        assert await cache_service.get("missing-hash") is None

    async def test_set_then_get_round_trips(self, cache_service):
        await cache_service.set("hash1", _SUMMARY)

        assert await cache_service.get("hash1") == _SUMMARY

    async def test_processing_lock_is_exclusive(self, cache_service):
        assert await cache_service.try_acquire_processing_lock("hash2") is True
        assert await cache_service.try_acquire_processing_lock("hash2") is False

    async def test_processing_lock_can_be_reacquired_after_release(self, cache_service):
        await cache_service.try_acquire_processing_lock("hash3")

        await cache_service.release_processing_lock("hash3")

        assert await cache_service.try_acquire_processing_lock("hash3") is True

    async def test_pop_waiters_returns_registered_waiters_and_clears_them(self, cache_service):
        await cache_service.register_waiter("hash4", "docA")
        await cache_service.register_waiter("hash4", "docB")

        waiters = await cache_service.pop_waiters("hash4")

        assert waiters == ["docA", "docB"]
        assert await cache_service.pop_waiters("hash4") == []


class TestCacheServiceGracefulDegradation:
    async def test_get_fails_open_as_a_miss_when_redis_is_unavailable(self, broken_redis_client):
        cache = CacheService(broken_redis_client, cache_ttl_seconds=60, lock_ttl_seconds=60)

        assert await cache.get("hash") is None

    async def test_set_does_not_raise_when_redis_is_unavailable(self, broken_redis_client):
        cache = CacheService(broken_redis_client, cache_ttl_seconds=60, lock_ttl_seconds=60)

        await cache.set("hash", _SUMMARY)  # must not raise

    async def test_processing_lock_fails_closed_when_redis_is_unavailable(self, broken_redis_client):
        # Unlike rate limiting, the lock fails *closed*: an unreachable
        # Redis must never be trusted to mean "you're the leader."
        cache = CacheService(broken_redis_client, cache_ttl_seconds=60, lock_ttl_seconds=60)

        assert await cache.try_acquire_processing_lock("hash") is False
