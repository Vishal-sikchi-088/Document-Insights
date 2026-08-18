import asyncio

from app.services.rate_limiter import RateLimiterService
from tests.conftest import TEST_MAX_ACTIVE_JOBS


class TestRateLimiterService:
    async def test_allows_up_to_the_configured_limit(self, rate_limiter):
        results = [await rate_limiter.try_acquire("u1", f"doc{i}") for i in range(TEST_MAX_ACTIVE_JOBS)]

        assert all(results)

    async def test_denies_once_limit_is_reached(self, rate_limiter):
        for i in range(TEST_MAX_ACTIVE_JOBS):
            await rate_limiter.try_acquire("u1", f"doc{i}")

        assert await rate_limiter.try_acquire("u1", "one-too-many") is False

    async def test_concurrent_acquires_never_exceed_the_limit(self, rate_limiter):
        # The real risk this guards against: a plain SCARD-then-SADD would
        # let multiple concurrent requests all read a count under the
        # limit before any of them writes — this asserts the atomic Lua
        # script actually closes that window.
        results = await asyncio.gather(
            *[rate_limiter.try_acquire("u1", f"doc{i}") for i in range(TEST_MAX_ACTIVE_JOBS + 2)]
        )

        assert sum(results) == TEST_MAX_ACTIVE_JOBS

    async def test_release_frees_a_slot(self, rate_limiter):
        for i in range(TEST_MAX_ACTIVE_JOBS):
            await rate_limiter.try_acquire("u1", f"doc{i}")

        await rate_limiter.release("u1", "doc0")

        assert await rate_limiter.try_acquire("u1", "new-doc") is True

    async def test_release_is_a_no_op_for_a_slot_never_acquired(self, rate_limiter):
        await rate_limiter.release("u1", "never-acquired")  # must not raise

    async def test_limits_are_independent_per_user(self, rate_limiter):
        for i in range(TEST_MAX_ACTIVE_JOBS):
            await rate_limiter.try_acquire("u1", f"doc{i}")

        assert await rate_limiter.try_acquire("u2", "doc0") is True


class TestRateLimiterGracefulDegradation:
    async def test_try_acquire_fails_open_when_redis_is_unavailable(self, broken_redis_client):
        limiter = RateLimiterService(broken_redis_client, max_active_jobs=3, entry_ttl_seconds=60)

        assert await limiter.try_acquire("u1", "doc1") is True

    async def test_release_does_not_raise_when_redis_is_unavailable(self, broken_redis_client):
        limiter = RateLimiterService(broken_redis_client, max_active_jobs=3, entry_ttl_seconds=60)

        await limiter.release("u1", "doc1")  # must not raise
