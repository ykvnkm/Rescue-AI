"""API health endpoint tests."""

from fastapi.testclient import TestClient

from rescue_ai.interfaces.api.app import app

client = TestClient(app)


def test_health_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
