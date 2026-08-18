"""Orchestrates document submission.

This is the one place that sequences the cache check, rate-limit
reservation, persistence, and enqueue-vs-wait decision described in the
assignment. Routers stay thin and only translate between HTTP and this
layer; the ordering and trade-offs below are business logic, not routing
concerns, so they live here.
"""
import logging
import math
from typing import Optional

from app.core.exceptions import DocumentNotFoundError, RateLimitExceededError
from app.core.hashing import compute_content_hash
from app.models.document import (
    DocumentCreateRequest,
    DocumentResponse,
    DocumentSubmitResponse,
    PaginatedDocumentsResponse,
)
from app.models.enums import DocumentStatus
from app.repositories.document_repository import DocumentRepository, generate_document_id
from app.services.cache_service import CacheService
from app.services.queue_service import QueueService
from app.services.rate_limiter import RateLimiterService

logger = logging.getLogger(__name__)


class DocumentService:
    def __init__(
        self,
        *,
        repository: DocumentRepository,
        rate_limiter: RateLimiterService,
        cache: CacheService,
        queue: QueueService,
    ):
        self._repository = repository
        self._rate_limiter = rate_limiter
        self._cache = cache
        self._queue = queue

    async def submit_document(self, request: DocumentCreateRequest) -> DocumentSubmitResponse:
        content_hash = compute_content_hash(request.content)

        # Content-based caching: identical content that's already been
        # processed by some earlier request completes immediately. This
        # document never occupies a rate-limit slot — from the caller's
        # perspective nothing is "in flight," so it shouldn't count against
        # their concurrent-job budget.
        cached_summary = await self._cache.get(content_hash)
        if cached_summary is not None:
            document = await self._repository.insert(
                document_id=generate_document_id(),
                user_id=request.user_id,
                title=request.title,
                content=request.content,
                content_hash=content_hash,
                status=DocumentStatus.COMPLETED,
                summary=cached_summary,
                cached=True,
            )
            logger.info(
                "document served from cache",
                extra={"document_id": document.id, "content_hash": content_hash},
            )
            return DocumentSubmitResponse(document_id=document.id, status=document.status)

        # The id is allocated client-side (no Mongo round-trip) so the rate
        # limit can be checked and reserved *before* any write happens — a
        # rejected submission should cost a Redis call, not a Mongo insert.
        document_id = generate_document_id()
        if not await self._rate_limiter.try_acquire(request.user_id, document_id):
            raise RateLimitExceededError(request.user_id)

        document = await self._repository.insert(
            document_id=document_id,
            user_id=request.user_id,
            title=request.title,
            content=request.content,
            content_hash=content_hash,
            status=DocumentStatus.QUEUED,
        )

        # Leader/follower coordination for two submissions of identical
        # *new* content racing in before either has a cached result yet —
        # the cache check above only catches content some earlier request
        # already finished processing, not this in-flight race.
        is_leader = await self._cache.try_acquire_processing_lock(content_hash)
        if is_leader:
            await self._queue.enqueue(document.id)
        else:
            # No queue entry for a follower: whichever worker finishes the
            # leader's job resolves every waiter directly once the result is
            # known (see SummarizerWorker), so identical in-flight content
            # is never processed twice concurrently. If the leader's job
            # ultimately fails, the worker promotes remaining waiters back
            # onto the real queue instead of leaving them stranded.
            await self._cache.register_waiter(content_hash, document.id)

        logger.info(
            "document submitted",
            extra={"document_id": document.id, "user_id": request.user_id, "is_leader": is_leader},
        )
        return DocumentSubmitResponse(document_id=document.id, status=document.status)

    async def get_document(self, document_id: str) -> DocumentResponse:
        # A malformed id and a well-formed-but-absent id are treated
        # identically as "not found" (the repository already collapses
        # them — see `_as_object_id`), rather than splitting them into 404
        # vs 400. A client only ever needs to know "does this id resolve,"
        # and not distinguishing the two avoids leaking id-format details.
        document = await self._repository.find_by_id(document_id)
        if document is None:
            raise DocumentNotFoundError(document_id)
        return DocumentResponse.from_db(document)

    async def list_user_documents(
        self,
        user_id: str,
        *,
        status: Optional[DocumentStatus],
        page: int,
        page_size: int,
    ) -> PaginatedDocumentsResponse:
        documents, total_items = await self._repository.find_by_user(
            user_id, status=status, page=page, page_size=page_size
        )
        return PaginatedDocumentsResponse(
            items=[DocumentResponse.from_db(document) for document in documents],
            page=page,
            page_size=page_size,
            total_items=total_items,
            total_pages=math.ceil(total_items / page_size),
        )
