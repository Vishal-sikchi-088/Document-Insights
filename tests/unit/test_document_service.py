import pytest

from app.core.exceptions import DocumentNotFoundError, RateLimitExceededError
from app.core.hashing import compute_content_hash
from app.models.document import DocumentCreateRequest, SummaryModel
from app.models.enums import DocumentStatus
from app.repositories.document_repository import generate_document_id
from app.services.cache_service import CacheService
from app.services.document_service import DocumentService
from tests.conftest import TEST_MAX_ACTIVE_JOBS


def _request(**overrides):
    defaults = {"user_id": "u1", "title": "T", "content": "some unique content"}
    defaults.update(overrides)
    return DocumentCreateRequest(**defaults)


class TestSubmitDocument:
    async def test_new_content_is_queued_and_enqueued(self, document_service, queue_service):
        response = await document_service.submit_document(_request(content="brand new content"))

        assert response.status == DocumentStatus.QUEUED
        assert await queue_service.dequeue(timeout_seconds=1) == response.document_id

    async def test_cache_hit_completes_immediately_without_enqueueing(
        self, document_service, cache_service, queue_service, rate_limiter
    ):
        content = "already summarized content"
        content_hash = compute_content_hash(content)
        summary = SummaryModel(summary_text="s", word_count=1, character_count=1, key_points=["s"])
        await cache_service.set(content_hash, summary)

        response = await document_service.submit_document(_request(user_id="u1", content=content))

        assert response.status == DocumentStatus.COMPLETED
        assert await queue_service.dequeue(timeout_seconds=1) is None
        # A cache hit must not occupy a rate-limit slot.
        assert await rate_limiter.try_acquire("u1", "probe") is True

    async def test_raises_when_user_is_at_the_active_job_limit(self, document_service):
        for i in range(TEST_MAX_ACTIVE_JOBS):
            await document_service.submit_document(_request(user_id="u1", content=f"distinct content {i}"))

        with pytest.raises(RateLimitExceededError):
            await document_service.submit_document(_request(user_id="u1", content="one too many"))

    async def test_second_submission_of_identical_new_content_becomes_a_waiter(
        self, document_service, queue_service, cache_service
    ):
        content = "racing duplicate content"

        leader = await document_service.submit_document(_request(user_id="u1", content=content))
        follower = await document_service.submit_document(_request(user_id="u2", content=content))

        assert await queue_service.dequeue(timeout_seconds=1) == leader.document_id
        waiters = await cache_service.pop_waiters(compute_content_hash(content))
        assert waiters == [follower.document_id]

    async def test_leader_resolves_via_cache_if_populated_between_miss_check_and_lock_win(
        self, document_repository, rate_limiter, queue_service, redis_client
    ):
        """Regression test for the double-checked-locking fix: if the
        cache is populated in the gap between the initial miss check and
        winning the processing lock, the "leader" must serve that result
        instead of committing to a redundant processing run.
        """
        content = "content that gets cached mid-race"
        content_hash = compute_content_hash(content)
        summary = SummaryModel(summary_text="s", word_count=1, character_count=1, key_points=["s"])

        class RaceSimulatingCache(CacheService):
            """Forces exactly one artificial miss, then defers to the real
            cache — simulating another request's write landing in the gap
            between our top-level check and winning the lock.
            """

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._get_calls = 0

            async def get(self, content_hash):
                self._get_calls += 1
                if self._get_calls == 1:
                    return None
                return await super().get(content_hash)

        cache = RaceSimulatingCache(redis_client, cache_ttl_seconds=3600, lock_ttl_seconds=120)
        await cache.set(content_hash, summary)
        service = DocumentService(
            repository=document_repository, rate_limiter=rate_limiter, cache=cache, queue=queue_service
        )

        response = await service.submit_document(_request(user_id="u1", content=content))

        assert response.status == DocumentStatus.COMPLETED
        assert await queue_service.dequeue(timeout_seconds=1) is None
        document = await document_repository.find_by_id(response.document_id)
        assert document.cached is True


class TestGetDocument:
    async def test_returns_response_for_existing_document(self, document_service):
        submitted = await document_service.submit_document(_request(content="lookup me"))

        found = await document_service.get_document(submitted.document_id)

        assert found.document_id == submitted.document_id
        assert found.status == DocumentStatus.QUEUED

    async def test_raises_not_found_for_absent_id(self, document_service):
        with pytest.raises(DocumentNotFoundError):
            await document_service.get_document("507f1f77bcf86cd799439011")

    async def test_raises_not_found_for_malformed_id(self, document_service):
        with pytest.raises(DocumentNotFoundError):
            await document_service.get_document("not-a-valid-id")


class TestListUserDocuments:
    async def test_returns_paginated_results_with_correct_totals(self, document_repository, document_service):
        for i in range(5):
            await document_repository.insert(
                document_id=generate_document_id(),
                user_id="paginated-user",
                title=f"D{i}",
                content=f"c{i}",
                content_hash=f"h{i}",
                status=DocumentStatus.QUEUED,
            )

        page = await document_service.list_user_documents(
            "paginated-user", status=None, page=1, page_size=2
        )

        assert len(page.items) == 2
        assert page.total_items == 5
        assert page.total_pages == 3

    async def test_returns_empty_page_for_unknown_user(self, document_service):
        page = await document_service.list_user_documents("nobody", status=None, page=1, page_size=20)

        assert page.items == []
        assert page.total_items == 0
