class TestHealthEndpoint:
    async def test_returns_200_when_dependencies_are_reachable(self, api_client):
        response = await api_client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["dependencies"] == {"mongodb": "ok", "redis": "ok"}
