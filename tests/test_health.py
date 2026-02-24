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


def test_version_ok() -> None:
    response = client.get("/version")
    assert response.status_code == 200
    assert response.json() == {"version": "0.1.0"}


def test_ui_index_ok() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Rescue Drone Station" in response.text
