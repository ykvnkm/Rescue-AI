"""API health endpoint tests."""

from fastapi.testclient import TestClient

from services.api_gateway.app import app

client = TestClient(app)


def test_health_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_ok() -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ui_index_ok() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Rescue Drone Station" in response.text
