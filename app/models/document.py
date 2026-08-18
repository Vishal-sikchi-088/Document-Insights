"""Pydantic schemas: the external API contract and the internal Mongo shape.

These are kept as two separate families of models on purpose:
- `DocumentInDB` mirrors exactly what's persisted — Mongo's `_id`, plus
  internal-only bookkeeping fields like `content_hash` and `attempts`.
- `DocumentCreateRequest` / `DocumentResponse` / `PaginatedDocumentsResponse`
  are the wire format and never leak those internal fields.

A change to how retries are tracked internally, for example, can't
accidentally change the public API response shape this way — the
`DocumentResponse.from_db` adapter is the one place that decides what
crosses the boundary.
"""
from datetime import datetime
from typing import Annotated, Any, Optional

from bson import ObjectId
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from app.models.enums import DocumentStatus


def _validate_object_id(value: Any) -> str:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, str) and ObjectId.is_valid(value):
        return value
    raise ValueError(f"{value!r} is not a valid ObjectId")


# Mongo's `_id` comes back from the driver as a `bson.ObjectId`; the API
# only ever deals in strings. Coercing at validation time here means every
# model that embeds an id gets consistent handling for free, instead of
# each call site remembering to `str()` it.
PyObjectId = Annotated[str, BeforeValidator(_validate_object_id)]


class SummaryModel(BaseModel):
    """The (mock) output of document processing."""

    summary_text: str
    word_count: int
    character_count: int
    key_points: list[str]


class DocumentCreateRequest(BaseModel):
    """Payload for `POST /documents`."""

    # Pydantic silently ignores unknown fields by default; forbidding them
    # turns a client's typo'd or misunderstood field name into an explicit
    # 422 instead of a request that looks accepted but quietly did less
    # than the caller expected.
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1, max_length=128)
    title: str = Field(..., min_length=1, max_length=300)
    content: str = Field(..., min_length=1, max_length=100_000)

    @field_validator("user_id", "title", "content")
    @classmethod
    def not_blank(cls, value: str) -> str:
        # min_length alone still lets "   " through, since Pydantic doesn't
        # strip strings by default — worth catching here rather than
        # letting a blank title reach Mongo.
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class DocumentInDB(BaseModel):
    """Mirrors exactly what's stored in the `documents` collection."""

    model_config = ConfigDict(populate_by_name=True)

    id: PyObjectId = Field(alias="_id")
    user_id: str
    title: str
    content: str
    content_hash: str
    status: DocumentStatus
    summary: Optional[SummaryModel] = None
    error: Optional[str] = None
    attempts: int = 0
    cached: bool = False
    created_at: datetime
    updated_at: datetime


class DocumentResponse(BaseModel):
    """External representation returned by the GET endpoints.

    Deliberately omits `content` and `content_hash`: a caller already has
    the content they submitted, and the hash is an implementation detail
    of the caching layer, not part of the public contract.
    """

    document_id: str
    user_id: str
    title: str
    status: DocumentStatus
    summary: Optional[SummaryModel] = None
    error: Optional[str] = None
    cached: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_db(cls, document: DocumentInDB) -> "DocumentResponse":
        return cls(
            document_id=document.id,
            user_id=document.user_id,
            title=document.title,
            status=document.status,
            summary=document.summary,
            error=document.error,
            cached=document.cached,
            created_at=document.created_at,
            updated_at=document.updated_at,
        )


class DocumentSubmitResponse(BaseModel):
    """Response for `POST /documents`.

    Deliberately slimmer than `DocumentResponse`: the spec calls for just
    the id and status, and a client about to poll `GET /documents/{id}`
    has no use for the rest of the fields yet.
    """

    document_id: str
    status: DocumentStatus


class PaginatedDocumentsResponse(BaseModel):
    """Response for `GET /users/{user_id}/documents`."""

    items: list[DocumentResponse]
    page: int
    page_size: int
    total_items: int
    total_pages: int
