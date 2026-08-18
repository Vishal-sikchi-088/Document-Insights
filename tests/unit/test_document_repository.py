import asyncio

from app.models.document import SummaryModel
from app.models.enums import DocumentStatus
from app.repositories.document_repository import generate_document_id


async def _insert_document(document_repository, **overrides):
    defaults = {
        "document_id": generate_document_id(),
        "user_id": "u1",
        "title": "T",
        "content": "c",
        "content_hash": "h",
        "status": DocumentStatus.QUEUED,
    }
    defaults.update(overrides)
    return await document_repository.insert(**defaults)


class TestFindById:
    async def test_returns_none_for_malformed_id(self, document_repository):
        assert await document_repository.find_by_id("not-an-object-id") is None

    async def test_returns_none_for_well_formed_but_absent_id(self, document_repository):
        assert await document_repository.find_by_id("507f1f77bcf86cd799439011") is None

    async def test_returns_inserted_document(self, document_repository):
        inserted = await _insert_document(document_repository)

        found = await document_repository.find_by_id(inserted.id)

        assert found is not None
        assert found.status == DocumentStatus.QUEUED


class TestClaimForProcessing:
    async def test_only_one_of_two_concurrent_claims_succeeds(self, document_repository):
        document = await _insert_document(document_repository)

        winner, loser = await asyncio.gather(
            document_repository.claim_for_processing(document.id),
            document_repository.claim_for_processing(document.id),
        )

        claims = [claim for claim in (winner, loser) if claim is not None]
        assert len(claims) == 1
        assert claims[0].status == DocumentStatus.PROCESSING
        assert claims[0].attempts == 1

    async def test_returns_none_when_document_not_in_queued_state(self, document_repository):
        document = await _insert_document(document_repository)
        await document_repository.claim_for_processing(document.id)  # -> processing

        second_claim = await document_repository.claim_for_processing(document.id)

        assert second_claim is None


class TestTerminalStateTransitions:
    async def test_mark_completed_sets_summary_and_status(self, document_repository):
        document = await _insert_document(document_repository)
        summary = SummaryModel(summary_text="s", word_count=1, character_count=1, key_points=["s"])

        await document_repository.mark_completed(document.id, summary)

        completed = await document_repository.find_by_id(document.id)
        assert completed.status == DocumentStatus.COMPLETED
        assert completed.summary.summary_text == "s"
        assert completed.cached is False

    async def test_mark_completed_records_cached_flag_when_resolved_as_waiter(self, document_repository):
        document = await _insert_document(document_repository)
        summary = SummaryModel(summary_text="s", word_count=1, character_count=1, key_points=["s"])

        await document_repository.mark_completed(document.id, summary, cached=True)

        resolved = await document_repository.find_by_id(document.id)
        assert resolved.cached is True

    async def test_mark_failed_then_requeue(self, document_repository):
        document = await _insert_document(document_repository)
        await document_repository.claim_for_processing(document.id)

        await document_repository.mark_failed(document.id, "simulated failure")
        failed = await document_repository.find_by_id(document.id)
        assert failed.status == DocumentStatus.FAILED
        assert failed.error == "simulated failure"

        await document_repository.requeue(document.id)
        requeued = await document_repository.find_by_id(document.id)
        assert requeued.status == DocumentStatus.QUEUED


class TestFindByUser:
    async def test_paginates_results_sorted_newest_first(self, document_repository):
        for i in range(5):
            await _insert_document(document_repository, user_id="u2", content_hash=f"h{i}")

        page1, total = await document_repository.find_by_user("u2", status=None, page=1, page_size=2)
        assert len(page1) == 2
        assert total == 5

        page3, _ = await document_repository.find_by_user("u2", status=None, page=3, page_size=2)
        assert len(page3) == 1

    async def test_filters_by_status(self, document_repository):
        for i in range(3):
            await _insert_document(document_repository, user_id="u3", content_hash=f"h{i}")

        matching, total = await document_repository.find_by_user(
            "u3", status=DocumentStatus.COMPLETED, page=1, page_size=10
        )
        assert matching == []
        assert total == 0

        matching, total = await document_repository.find_by_user(
            "u3", status=DocumentStatus.QUEUED, page=1, page_size=10
        )
        assert total == 3

    async def test_scopes_results_to_the_given_user(self, document_repository):
        await _insert_document(document_repository, user_id="only-user")

        _, total = await document_repository.find_by_user("someone-else", status=None, page=1, page_size=10)

        assert total == 0
