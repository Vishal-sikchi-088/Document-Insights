from datetime import datetime, timezone

import pytest
from bson import ObjectId
from pydantic import ValidationError

from app.models.document import DocumentCreateRequest, DocumentInDB, DocumentResponse
from app.models.enums import DocumentStatus


def _raw_mongo_document(**overrides):
    raw = {
        "_id": ObjectId(),
        "user_id": "u1",
        "title": "My Doc",
        "content": "hello world",
        "content_hash": "abc123",
        "status": "completed",
        "summary": None,
        "error": None,
        "attempts": 0,
        "cached": False,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    raw.update(overrides)
    return raw


def test_document_in_db_coerces_object_id_to_str():
    document = DocumentInDB.model_validate(_raw_mongo_document())

    assert isinstance(document.id, str)
    assert ObjectId.is_valid(document.id)


def test_document_in_db_rejects_invalid_object_id_string():
    with pytest.raises(ValidationError):
        DocumentInDB.model_validate(_raw_mongo_document(_id="not-an-object-id"))


def test_document_response_from_db_omits_internal_fields():
    document = DocumentInDB.model_validate(_raw_mongo_document())

    response = DocumentResponse.from_db(document)

    assert response.document_id == document.id
    dumped = response.model_dump()
    assert "content" not in dumped
    assert "content_hash" not in dumped
    assert "attempts" not in dumped


class TestDocumentCreateRequestValidation:
    def test_accepts_well_formed_payload_and_strips_whitespace(self):
        request = DocumentCreateRequest(user_id=" u1 ", title=" Title ", content=" body ")

        assert request.user_id == "u1"
        assert request.title == "Title"
        assert request.content == "body"

    @pytest.mark.parametrize("field", ["user_id", "title", "content"])
    def test_rejects_whitespace_only_field(self, field):
        payload = {"user_id": "u1", "title": "T", "content": "c"}
        payload[field] = "   "

        with pytest.raises(ValidationError):
            DocumentCreateRequest(**payload)

    def test_rejects_unknown_fields(self):
        with pytest.raises(ValidationError):
            DocumentCreateRequest(user_id="u1", title="T", content="c", priority="high")

    def test_rejects_content_over_max_length(self):
        with pytest.raises(ValidationError):
            DocumentCreateRequest(user_id="u1", title="T", content="x" * 100_001)
