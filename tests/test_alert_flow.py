"""Mission and alert flow API tests."""

from fastapi.testclient import TestClient

from services.api_gateway.app import app
from services.api_gateway.infrastructure import memory_store

client = TestClient(app)


def setup_function() -> None:
    memory_store.reset_state()


def test_create_mission() -> None:
    response = client.post("/v1/missions")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "created"
    assert isinstance(payload["mission_id"], str)
    assert payload["mission_id"]


def test_get_alerts_filtered_by_mission() -> None:
    mission_a = client.post("/v1/missions").json()["mission_id"]
    mission_b = client.post("/v1/missions").json()["mission_id"]

    memory_store.add_alert(
        mission_id=mission_a,
        frame_id=10,
        ts_sec=1.1,
        score=0.91,
    )
    memory_store.add_alert(
        mission_id=mission_b,
        frame_id=20,
        ts_sec=2.2,
        score=0.85,
    )

    response = client.get(f"/v1/alerts?mission_id={mission_a}")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["mission_id"] == mission_a


def test_confirm_and_reject_alert() -> None:
    mission_id = client.post("/v1/missions").json()["mission_id"]
    alert_confirm = memory_store.add_alert(
        mission_id=mission_id,
        frame_id=1,
        ts_sec=0.5,
        score=0.95,
    )
    alert_reject = memory_store.add_alert(
        mission_id=mission_id,
        frame_id=2,
        ts_sec=0.8,
        score=0.60,
    )

    confirm_response = client.post(
        f"/v1/alerts/{alert_confirm.alert_id}/confirm",
        json={"reviewed_by": "operator_1"},
    )
    reject_response = client.post(
        f"/v1/alerts/{alert_reject.alert_id}/reject",
        json={"reviewed_by": "operator_1"},
    )

    assert confirm_response.status_code == 200
    assert confirm_response.json()["status"] == "reviewed_confirmed"
    assert reject_response.status_code == 200
    assert reject_response.json()["status"] == "reviewed_rejected"


def test_confirm_not_found() -> None:
    response = client.post(
        "/v1/alerts/not-exists/confirm",
        json={"reviewed_by": "operator_1"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Alert not found"
