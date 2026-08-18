import asyncio

from app.models.document import DocumentCreateRequest
from app.models.enums import DocumentStatus
from app.workers.summarizer_worker import SummarizerWorker


def _worker(document_repository, rate_limiter, cache_service, queue_service, *, failure_rate, max_retries, backoff=0):
    return SummarizerWorker(
        repository=document_repository,
        rate_limiter=rate_limiter,
        cache=cache_service,
        queue=queue_service,
        min_processing_seconds=0,
        max_processing_seconds=0,
        failure_rate=failure_rate,
        max_retries=max_retries,
        retry_backoff_seconds=backoff,
    )


class TestSuccessfulProcessing:
    async def test_completes_document_and_populates_cache(
        self, document_service, document_repository, rate_limiter, cache_service, queue_service
    ):
        response = await document_service.submit_document(
            DocumentCreateRequest(user_id="u1", title="T", content="content to summarize")
        )
        job_id = await queue_service.dequeue(timeout_seconds=1)
        worker = _worker(document_repository, rate_limiter, cache_service, queue_service, failure_rate=0.0, max_retries=2)

        await worker._process_job(job_id)

        document = await document_repository.find_by_id(response.document_id)
        assert document.status == DocumentStatus.COMPLETED
        assert document.summary is not None
        assert document.cached is False
        assert await cache_service.get(document.content_hash) == document.summary
        assert await rate_limiter.try_acquire("u1", "probe") is True  # slot released

    async def test_resolves_a_waiting_follower_with_the_leaders_result(
        self, document_service, document_repository, rate_limiter, cache_service, queue_service
    ):
        content = "shared content for leader/follower test"
        leader = await document_service.submit_document(DocumentCreateRequest(user_id="leader", title="A", content=content))
        follower = await document_service.submit_document(DocumentCreateRequest(user_id="follower", title="B", content=content))
        job_id = await queue_service.dequeue(timeout_seconds=1)
        assert job_id == leader.document_id
        worker = _worker(document_repository, rate_limiter, cache_service, queue_service, failure_rate=0.0, max_retries=2)

        await worker._process_job(job_id)

        leader_doc = await document_repository.find_by_id(leader.document_id)
        follower_doc = await document_repository.find_by_id(follower.document_id)
        assert follower_doc.status == DocumentStatus.COMPLETED
        assert follower_doc.cached is True
        assert follower_doc.summary == leader_doc.summary
        assert await rate_limiter.try_acquire("follower", "probe") is True  # follower's slot released too


class TestRetryAndFailure:
    async def test_failure_within_retry_budget_requeues_instead_of_failing(
        self, document_service, document_repository, rate_limiter, cache_service, queue_service
    ):
        response = await document_service.submit_document(
            DocumentCreateRequest(user_id="u1", title="T", content="will fail once")
        )
        job_id = await queue_service.dequeue(timeout_seconds=1)
        worker = _worker(
            document_repository, rate_limiter, cache_service, queue_service, failure_rate=1.0, max_retries=1, backoff=0
        )

        await worker._process_job(job_id)

        document = await document_repository.find_by_id(response.document_id)
        assert document.status == DocumentStatus.QUEUED
        assert document.attempts == 1

        await asyncio.gather(*worker._background_tasks, return_exceptions=True)
        assert await queue_service.dequeue(timeout_seconds=1) == response.document_id

    async def test_failure_past_retry_budget_marks_failed_and_releases_slot(
        self, document_service, document_repository, rate_limiter, cache_service, queue_service
    ):
        response = await document_service.submit_document(
            DocumentCreateRequest(user_id="u1", title="T", content="will always fail")
        )
        job_id = await queue_service.dequeue(timeout_seconds=1)
        worker = _worker(document_repository, rate_limiter, cache_service, queue_service, failure_rate=1.0, max_retries=0)

        await worker._process_job(job_id)

        document = await document_repository.find_by_id(response.document_id)
        assert document.status == DocumentStatus.FAILED
        assert document.error is not None
        assert await rate_limiter.try_acquire("u1", "probe") is True

    async def test_leader_failure_promotes_follower_to_an_independent_job(
        self, document_service, document_repository, rate_limiter, cache_service, queue_service
    ):
        content = "content Y whose leader will fail"
        leader = await document_service.submit_document(DocumentCreateRequest(user_id="leader2", title="D", content=content))
        follower = await document_service.submit_document(DocumentCreateRequest(user_id="follower2", title="E", content=content))
        job_id = await queue_service.dequeue(timeout_seconds=1)
        assert job_id == leader.document_id
        failing_worker = _worker(document_repository, rate_limiter, cache_service, queue_service, failure_rate=1.0, max_retries=0)

        await failing_worker._process_job(job_id)

        leader_doc = await document_repository.find_by_id(leader.document_id)
        assert leader_doc.status == DocumentStatus.FAILED

        promoted_job_id = await queue_service.dequeue(timeout_seconds=1)
        assert promoted_job_id == follower.document_id

        succeeding_worker = _worker(document_repository, rate_limiter, cache_service, queue_service, failure_rate=0.0, max_retries=0)
        await succeeding_worker._process_job(promoted_job_id)

        follower_doc = await document_repository.find_by_id(follower.document_id)
        assert follower_doc.status == DocumentStatus.COMPLETED
        assert follower_doc.cached is False  # processed independently, not resolved from a leader


class TestGracefulShutdown:
    async def test_run_returns_promptly_when_stop_event_is_already_set(
        self, document_repository, rate_limiter, cache_service, queue_service
    ):
        worker = _worker(document_repository, rate_limiter, cache_service, queue_service, failure_rate=0.0, max_retries=0)
        stop_event = asyncio.Event()
        stop_event.set()

        await asyncio.wait_for(worker.run(stop_event), timeout=3)
