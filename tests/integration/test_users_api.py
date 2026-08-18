"""Integration tests for GET /users/{user_id}/documents.

Data is seeded directly through the repository rather than via POST
/documents, so pagination/filtering behavior is tested independently of
rate-limiting behavior (already covered in test_documents_api.py).
"""
from app.models.enums import DocumentStatus
from app.repositories.document_repository import DocumentRepository, generate_document_id


async def _seed_documents(mongo_db, user_id: str, count: int) -> None:
    repository = DocumentRepository(mongo_db)
    for i in range(count):
        await repository.insert(
            document_id=generate_document_id(),
            user_id=user_id,
            title=f"D{i}",
            content=f"content {i}",
            content_hash=f"hash-{user_id}-{i}",
            status=DocumentStatus.QUEUED,
        )


class TestListUserDocuments:
    async def test_paginates_results(self, api_client, mongo_db):
        await _seed_documents(mongo_db, "frank", count=7)

        response = await api_client.get("/users/frank/documents", params={"page": 1, "page_size": 3})

        assert response.status_code == 200
        body = response.json()
        assert len(body["items"]) == 3
        assert body["total_items"] == 7
        assert body["total_pages"] == 3

    async def test_filters_by_status(self, api_client, mongo_db):
        await _seed_documents(mongo_db, "grace", count=3)

        completed = await api_client.get("/users/grace/documents", params={"status": "completed"})
        queued = await api_client.get("/users/grace/documents", params={"status": "queued"})

        assert completed.json()["total_items"] == 0
        assert queued.json()["total_items"] == 3

    async def test_returns_422_for_an_invalid_status_value(self, api_client):
        response = await api_client.get("/users/grace/documents", params={"status": "bogus"})

        assert response.status_code == 422

    async def test_returns_422_for_page_size_beyond_the_configured_maximum(self, api_client):
        response = await api_client.get("/users/grace/documents", params={"page_size": 99999})

        assert response.status_code == 422

    async def test_returns_empty_page_for_an_unknown_user(self, api_client):
        response = await api_client.get("/users/nobody/documents")

        assert response.status_code == 200
        assert response.json()["total_items"] == 0
