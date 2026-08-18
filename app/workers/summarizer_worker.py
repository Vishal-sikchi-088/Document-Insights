"""Background worker: claims queued documents and drives them through
(mock) processing to a terminal state.

Runs as its own process (see `main()` below, and the `worker` service in
docker-compose), decoupled from the API process. Horizontal scaling is
just running more replicas of this same loop — `BLPOP`'s atomic delivery
plus `DocumentRepository.claim_for_processing`'s atomic status transition
are what make that safe without any coordination between replicas beyond
what Redis and Mongo already guarantee.
"""
import asyncio
import logging
import random
import signal

from app.config import get_settings
from app.core.ttl import compute_worst_case_job_lifetime_seconds
from app.db.mongo import create_indexes, create_mongo_client
from app.db.redis import create_redis_client
from app.logging_config import configure_logging
from app.models.document import DocumentInDB, SummaryModel
from app.repositories.document_repository import DocumentRepository
from app.services.cache_service import CacheService
from app.services.queue_service import QueueService
from app.services.rate_limiter import RateLimiterService
from app.services.summarization import generate_mock_summary

logger = logging.getLogger(__name__)

# How long a single BLPOP call blocks before returning empty-handed, so the
# loop wakes periodically to check the shutdown signal instead of blocking
# indefinitely against an idle queue.
_DEQUEUE_POLL_SECONDS = 5


class SummarizerWorker:
    def __init__(
        self,
        *,
        repository: DocumentRepository,
        rate_limiter: RateLimiterService,
        cache: CacheService,
        queue: QueueService,
        min_processing_seconds: int,
        max_processing_seconds: int,
        failure_rate: float,
        max_retries: int,
        retry_backoff_seconds: int,
    ):
        self._repository = repository
        self._rate_limiter = rate_limiter
        self._cache = cache
        self._queue = queue
        self._min_processing_seconds = min_processing_seconds
        self._max_processing_seconds = max_processing_seconds
        self._failure_rate = failure_rate
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        # Retry backoff happens off the main loop (see `_schedule_retry`) so
        # one document's backoff delay never blocks the worker from picking
        # up the next queued job. Tracked here purely so shutdown can wait
        # for them instead of abandoning a scheduled retry mid-backoff.
        self._background_tasks: set[asyncio.Task] = set()

    async def run(self, stop_event: asyncio.Event) -> None:
        logger.info("worker started")
        while not stop_event.is_set():
            document_id = await self._queue.dequeue(timeout_seconds=_DEQUEUE_POLL_SECONDS)
            if document_id is None:
                continue
            try:
                await self._process_job(document_id)
            except Exception:
                # A bug here must never take the worker loop down with it —
                # log the full traceback and keep serving the queue. The
                # document is left in whatever state `_process_job` reached
                # (most likely still `processing`); see the README for the
                # stale-document reconciliation sweep this points at.
                logger.exception("unexpected error processing job", extra={"document_id": document_id})

        logger.info("worker stopping, waiting for scheduled retries to flush")
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        logger.info("worker stopped")

    async def _process_job(self, document_id: str) -> None:
        document = await self._repository.claim_for_processing(document_id)
        if document is None:
            # Lost the race, or the queue delivered an id no longer in a
            # claimable state (already resolved by a duplicate delivery,
            # for instance). Nothing to do.
            logger.info("job not claimable, skipping", extra={"document_id": document_id})
            return

        logger.info("processing started", extra={"document_id": document.id, "attempt": document.attempts})
        await asyncio.sleep(random.uniform(self._min_processing_seconds, self._max_processing_seconds))

        if random.random() < self._failure_rate:
            await self._handle_failure(document)
        else:
            await self._handle_success(document)

    async def _handle_success(self, document: DocumentInDB) -> None:
        summary = generate_mock_summary(document.content)
        await self._repository.mark_completed(document.id, summary)
        await self._rate_limiter.release(document.user_id, document.id)

        # Cache the result and resolve anyone waiting on this exact content
        # before releasing the lock, so a request arriving in that narrow
        # window sees a cache hit rather than a lock it's free to acquire.
        await self._cache.set(document.content_hash, summary)
        await self._resolve_waiters(document.content_hash, summary)
        await self._cache.release_processing_lock(document.content_hash)

        logger.info("processing completed", extra={"document_id": document.id})

    async def _handle_failure(self, document: DocumentInDB) -> None:
        if document.attempts <= self._max_retries:
            backoff_seconds = self._retry_backoff_seconds * (2 ** (document.attempts - 1))
            logger.warning(
                "processing failed, scheduling retry",
                extra={"document_id": document.id, "attempt": document.attempts, "retry_in_seconds": backoff_seconds},
            )
            await self._repository.requeue(document.id)
            self._schedule_retry(document.id, backoff_seconds)
            return

        logger.warning(
            "processing failed, retries exhausted",
            extra={"document_id": document.id, "attempts": document.attempts},
        )
        await self._repository.mark_failed(document.id, "simulated processing failure")
        await self._rate_limiter.release(document.user_id, document.id)
        await self._cache.release_processing_lock(document.content_hash)
        # The leader never produced a result to cache, so followers can't
        # be resolved from it — the only correct move is giving each of
        # them an independent shot at processing rather than leaving them
        # queued forever with no entry in the actual job queue.
        await self._promote_waiters(document.content_hash)

    def _schedule_retry(self, document_id: str, delay_seconds: float) -> None:
        task = asyncio.create_task(self._delayed_requeue(document_id, delay_seconds))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _delayed_requeue(self, document_id: str, delay_seconds: float) -> None:
        await asyncio.sleep(delay_seconds)
        await self._queue.enqueue(document_id)

    async def _resolve_waiters(self, content_hash: str, summary: SummaryModel) -> None:
        waiter_ids = await self._cache.pop_waiters(content_hash)
        for waiter_id in waiter_ids:
            waiter = await self._repository.find_by_id(waiter_id)
            if waiter is None:
                # Defensive only: nothing in this codebase deletes
                # documents, so this should be unreachable outside of
                # manual DB intervention.
                logger.warning("waiter document vanished before it could be resolved", extra={"document_id": waiter_id})
                continue
            await self._repository.mark_completed(waiter_id, summary, cached=True)
            await self._rate_limiter.release(waiter.user_id, waiter_id)
            logger.info(
                "waiter resolved from leader's result",
                extra={"document_id": waiter_id, "content_hash": content_hash},
            )

    async def _promote_waiters(self, content_hash: str) -> None:
        waiter_ids = await self._cache.pop_waiters(content_hash)
        for waiter_id in waiter_ids:
            await self._queue.enqueue(waiter_id)
            logger.info(
                "waiter promoted to independent job after leader failure",
                extra={"document_id": waiter_id, "content_hash": content_hash},
            )


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    mongo_client = create_mongo_client(settings.mongodb_uri)
    db = mongo_client[settings.mongodb_db_name]
    await create_indexes(db)
    redis_client = create_redis_client(settings.redis_url)

    ttl_seconds = compute_worst_case_job_lifetime_seconds(settings)
    worker = SummarizerWorker(
        repository=DocumentRepository(db),
        rate_limiter=RateLimiterService(
            redis_client, max_active_jobs=settings.rate_limit_max_active_jobs, entry_ttl_seconds=ttl_seconds
        ),
        cache=CacheService(redis_client, cache_ttl_seconds=settings.cache_ttl_seconds, lock_ttl_seconds=ttl_seconds),
        queue=QueueService(redis_client, queue_key=settings.redis_queue_key),
        min_processing_seconds=settings.worker_min_processing_seconds,
        max_processing_seconds=settings.worker_max_processing_seconds,
        failure_rate=settings.worker_failure_rate,
        max_retries=settings.worker_max_retries,
        retry_backoff_seconds=settings.worker_retry_backoff_seconds,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    logger.info("worker process starting", extra={"environment": settings.environment})
    try:
        await worker.run(stop_event)
    finally:
        mongo_client.close()
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
