"""Content-hash based summary caching.

Also holds the primitives behind the "handling concurrent duplicate
submissions" bonus: a short-lived per-hash lock plus a waiters list, used
by DocumentService/the worker so that two requests racing in with
identical content don't both pay for a full 10-30s mock processing run
when one cached result would satisfy both.
"""
import json
import logging
from typing import Optional

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.redis_keys import cache_key, lock_key, waiters_key
from app.models.document import SummaryModel

logger = logging.getLogger(__name__)


class CacheService:
    def __init__(self, client: Redis, *, cache_ttl_seconds: int, lock_ttl_seconds: int):
        self._client = client
        self._cache_ttl_seconds = cache_ttl_seconds
        # Deliberately separate from cache_ttl_seconds: this bounds how long
        # an in-flight "leader" job may coordinate followers, which is a
        # function of worst-case processing time, not of the cache's
        # freshness policy. Conflating the two would mean a long cache TTL
        # (a business decision) accidentally holding a stale lock for hours.
        self._lock_ttl_seconds = lock_ttl_seconds

    async def get(self, content_hash: str) -> Optional[SummaryModel]:
        """Look up a previously computed summary.

        Fails open (reports a cache miss) on a Redis outage — a miss just
        routes the document through normal processing, which is always
        correct, only slower. There's no failure-closed option here that
        would make sense.
        """
        try:
            raw = await self._client.get(cache_key(content_hash))
        except RedisError:
            logger.warning("content cache unavailable, treating as miss", extra={"content_hash": content_hash})
            return None
        if raw is None:
            return None
        return SummaryModel.model_validate(json.loads(raw))

    async def set(self, content_hash: str, summary: SummaryModel) -> None:
        """Best-effort cache write. A failure here shouldn't fail a request
        that just finished real processing, so it's logged and swallowed
        rather than propagated — the next submission for this content will
        simply miss the cache and reprocess.
        """
        try:
            await self._client.set(
                cache_key(content_hash), summary.model_dump_json(), ex=self._cache_ttl_seconds
            )
        except RedisError:
            logger.warning("failed to write content cache entry", extra={"content_hash": content_hash})

    async def try_acquire_processing_lock(self, content_hash: str) -> bool:
        """Leader election for concurrent identical-content submissions.

        The first request for a given content hash becomes the "leader"
        that actually enqueues a processing job; concurrent followers
        register as waiters instead of each triggering their own
        redundant 10-30s run for content that's about to be cached anyway.
        `SET NX` is atomic, so exactly one caller wins under concurrency.

        Fails *closed* (returns False) on a Redis outage, unlike the rest
        of this class: the caller's fallback when it isn't the leader is
        just to process independently, which is always correct — so
        there's no reason to trust an unreachable Redis with "yes, you're
        the leader."
        """
        try:
            acquired = await self._client.set(
                lock_key(content_hash), "1", nx=True, ex=self._lock_ttl_seconds
            )
            return bool(acquired)
        except RedisError:
            logger.warning("processing lock unavailable, proceeding independently", extra={"content_hash": content_hash})
            return False

    async def release_processing_lock(self, content_hash: str) -> None:
        """Release early once the leader's result is cached, so a
        newly-arriving request doesn't wait out the rest of the TTL
        needlessly. Not releasing (e.g. on a crash) just means the lock
        expires on its own — the TTL is the correctness backstop.
        """
        try:
            await self._client.delete(lock_key(content_hash))
        except RedisError:
            logger.warning("failed to release processing lock; will expire via TTL", extra={"content_hash": content_hash})

    async def register_waiter(self, content_hash: str, document_id: str) -> None:
        try:
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.rpush(waiters_key(content_hash), document_id)
                pipe.expire(waiters_key(content_hash), self._lock_ttl_seconds)
                await pipe.execute()
        except RedisError:
            logger.warning(
                "failed to register cache waiter; document will fall back "
                "to independent processing rather than being resolved by the leader",
                extra={"content_hash": content_hash, "document_id": document_id},
            )

    async def pop_waiters(self, content_hash: str) -> list[str]:
        """Atomically fetch and clear every document waiting on this
        content hash's result. Wrapped in a transaction so exactly one
        caller (whichever worker finishes the leader job) ever resolves a
        given waiter — never zero, never twice.
        """
        try:
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.lrange(waiters_key(content_hash), 0, -1)
                pipe.delete(waiters_key(content_hash))
                waiters, _ = await pipe.execute()
            return waiters
        except RedisError:
            logger.warning("failed to read cache waiters", extra={"content_hash": content_hash})
            return []
