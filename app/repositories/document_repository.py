"""Data access for the `documents` collection.

This is the only module that speaks Mongo query syntax; services depend on
this repository's method signatures, not on `find_one_and_update` filter
shapes. That boundary is what makes the race-condition guard in
`claim_for_processing` reusable and independently testable.
"""
import asyncio
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument

from app.db.mongo import DOCUMENTS_COLLECTION
from app.models.document import DocumentInDB, SummaryModel
from app.models.enums import DocumentStatus


def _as_object_id(document_id: str) -> Optional[ObjectId]:
    # Mongo raises on a malformed ObjectId string rather than just failing
    # to match, so every lookup needs this guard first — a client sending
    # a garbage id should see "not found," not a 500.
    return ObjectId(document_id) if ObjectId.is_valid(document_id) else None


class DocumentRepository:
    def __init__(self, db: AsyncIOMotorDatabase):
        self._collection = db.get_collection(DOCUMENTS_COLLECTION)

    async def insert(
        self,
        *,
        user_id: str,
        title: str,
        content: str,
        content_hash: str,
        status: DocumentStatus,
        summary: Optional[SummaryModel] = None,
        cached: bool = False,
    ) -> DocumentInDB:
        now = datetime.now(timezone.utc)
        raw = {
            "user_id": user_id,
            "title": title,
            "content": content,
            "content_hash": content_hash,
            "status": status.value,
            "summary": summary.model_dump() if summary else None,
            "error": None,
            "attempts": 0,
            "cached": cached,
            "created_at": now,
            "updated_at": now,
        }
        result = await self._collection.insert_one(raw)
        raw["_id"] = result.inserted_id
        return DocumentInDB.model_validate(raw)

    async def find_by_id(self, document_id: str) -> Optional[DocumentInDB]:
        object_id = _as_object_id(document_id)
        if object_id is None:
            return None
        raw = await self._collection.find_one({"_id": object_id})
        return DocumentInDB.model_validate(raw) if raw else None

    async def find_by_user(
        self,
        user_id: str,
        *,
        status: Optional[DocumentStatus],
        page: int,
        page_size: int,
    ) -> tuple[list[DocumentInDB], int]:
        query: dict = {"user_id": user_id}
        if status is not None:
            query["status"] = status.value
        skip = (page - 1) * page_size

        async def fetch_page() -> list[DocumentInDB]:
            cursor = (
                self._collection.find(query)
                .sort("created_at", -1)
                .skip(skip)
                .limit(page_size)
            )
            return [DocumentInDB.model_validate(raw) async for raw in cursor]

        # The page of results and the total count are independent reads —
        # running them concurrently halves the latency of this endpoint
        # versus awaiting them one after the other.
        documents, total = await asyncio.gather(
            fetch_page(), self._collection.count_documents(query)
        )
        return documents, total

    async def claim_for_processing(self, document_id: str) -> Optional[DocumentInDB]:
        """Atomically transition a document from `queued` to `processing`.

        This is the race-condition guard called out in the assignment: if
        the same document_id were ever dequeued twice (a crashed worker's
        job redelivered, a duplicate enqueue), only the first caller's
        `find_one_and_update` matches a document still in `queued` state.
        The second call matches nothing and gets back `None` instead of
        both workers processing the same document concurrently — no
        separate distributed lock needed, Mongo's atomic update *is* the
        lock.
        """
        object_id = _as_object_id(document_id)
        if object_id is None:
            return None
        raw = await self._collection.find_one_and_update(
            {"_id": object_id, "status": DocumentStatus.QUEUED.value},
            {
                "$set": {
                    "status": DocumentStatus.PROCESSING.value,
                    "updated_at": datetime.now(timezone.utc),
                },
                "$inc": {"attempts": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        return DocumentInDB.model_validate(raw) if raw else None

    async def mark_completed(self, document_id: str, summary: SummaryModel) -> None:
        await self._collection.update_one(
            {"_id": _as_object_id(document_id)},
            {
                "$set": {
                    "status": DocumentStatus.COMPLETED.value,
                    "summary": summary.model_dump(),
                    "error": None,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

    async def mark_failed(self, document_id: str, error: str) -> None:
        await self._collection.update_one(
            {"_id": _as_object_id(document_id)},
            {
                "$set": {
                    "status": DocumentStatus.FAILED.value,
                    "error": error,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

    async def requeue(self, document_id: str) -> None:
        """Reset a document back to `queued` for a retry attempt.

        Separate from `mark_failed`: a job that's being retried isn't in a
        failed state yet, it's going back to the front of the line. Only
        the worker's retry loop, once attempts are exhausted, calls
        `mark_failed` instead of this.
        """
        await self._collection.update_one(
            {"_id": _as_object_id(document_id)},
            {
                "$set": {
                    "status": DocumentStatus.QUEUED.value,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
