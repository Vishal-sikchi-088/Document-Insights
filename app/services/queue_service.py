"""Redis-list-backed job queue for the background worker.

Unlike the rate limiter and cache, this is not a candidate for graceful
degradation: it *is* the mechanism by which a queued document ever gets
processed. If Redis is unreachable here, that should surface as a loud
failure on submission (the router turns it into a 503) rather than
silently accepting a document that no worker will ever pick up.
"""
from typing import Optional

from redis.asyncio import Redis


class QueueService:
    def __init__(self, client: Redis, *, queue_key: str):
        self._client = client
        self._queue_key = queue_key

    async def enqueue(self, document_id: str) -> None:
        await self._client.rpush(self._queue_key, document_id)

    async def dequeue(self, timeout_seconds: int) -> Optional[str]:
        """Block up to `timeout_seconds` waiting for a job.

        `BLPOP` is atomic: when multiple worker processes call this
        against the same list, each queued document_id is delivered to
        exactly one of them. That's what rules out two workers picking up
        the same job at the queue level — `DocumentRepository
        .claim_for_processing`'s atomic status transition is the second,
        independent guard for the edge case where a document_id somehow
        ends up delivered twice (e.g. a manual requeue racing a redelivery).

        Returns None on timeout, so the worker's loop wakes periodically
        instead of blocking forever and can check for a shutdown signal.
        """
        result = await self._client.blpop([self._queue_key], timeout=timeout_seconds)
        if result is None:
            return None
        _, document_id = result
        return document_id
