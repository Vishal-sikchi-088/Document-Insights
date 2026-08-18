"""Integration tests for POST /documents and GET /documents/{id}.

Runs the real FastAPI app (see `api_client` in conftest.py) — only the
Mongo/Redis clients are swapped for in-memory fakes.
"""
from app.core.hashing import compute_content_hash
from app.core.redis_keys import cache_key
from app.models.document import SummaryModel


async def _submit(api_client, **overrides):
    payload = {"user_id": "u1", "title": "T", "content": "default content"}
    payload.update(overrides)
    return await api_client.post("/documents", json=payload)


class TestSubmitDocument:
    async def test_returns_201_with_document_id_and_queued_status(self, api_client):
        response = await _submit(api_client, content="unique content one")

        assert response.status_code == 201
        body = response.json()
        assert "document_id" in body
        assert body["status"] == "queued"

    async def test_returns_422_for_blank_title(self, api_client):
        response = await _submit(api_client, title="   ")

        assert response.status_code == 422

    async def test_returns_422_for_unexpected_fields(self, api_client):
        response = await _submit(api_client, priority="high")

        assert response.status_code == 422

    async def test_returns_429_after_exceeding_the_active_job_limit(self, api_client):
        for i in range(3):
            response = await _submit(api_client, user_id="rate-limited-user", content=f"distinct {i}")
            assert response.status_code == 201

        response = await _submit(api_client, user_id="rate-limited-user", content="one too many")

        assert response.status_code == 429

    async def test_cache_hit_returns_completed_immediately(self, api_client, redis_client):
        content = "content with a precomputed summary"
        content_hash = compute_content_hash(content)
        summary = SummaryModel(summary_text="cached", word_count=2, character_count=30, key_points=["a"])
        await redis_client.set(cache_key(content_hash), summary.model_dump_json())

        response = await _submit(api_client, user_id="cache-user", content=content)

        assert response.status_code == 201
        assert response.json()["status"] == "completed"


class TestGetDocument:
    async def test_returns_200_with_full_document_for_a_known_id(self, api_client):
        submit_response = await _submit(api_client, user_id="lookup-user", content="find me later")
        document_id = submit_response.json()["document_id"]

        response = await api_client.get(f"/documents/{document_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["document_id"] == document_id
        assert body["status"] == "queued"
        assert body["summary"] is None

    async def test_returns_404_for_a_well_formed_but_absent_id(self, api_client):
        response = await api_client.get("/documents/507f1f77bcf86cd799439011")

        assert response.status_code == 404

    async def test_returns_404_for_a_malformed_id(self, api_client):
        response = await api_client.get("/documents/not-a-valid-id")

        assert response.status_code == 404
