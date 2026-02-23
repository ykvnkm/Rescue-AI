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


def test_ingest_frame_creates_alert_after_quorum() -> None:
    mission_id = client.post("/v1/missions").json()["mission_id"]

    first_response = client.post(
        "/v1/frames",
        json={
            "mission_id": mission_id,
            "frame_id": 1,
            "ts_sec": 0.0,
            "score": 0.95,
            "gt_person_present": True,
        },
    )
    second_response = client.post(
        "/v1/frames",
        json={
            "mission_id": mission_id,
            "frame_id": 2,
            "ts_sec": 0.4,
            "score": 0.93,
            "gt_person_present": True,
        },
    )
    third_response = client.post(
        "/v1/frames",
        json={
            "mission_id": mission_id,
            "frame_id": 3,
            "ts_sec": 0.8,
            "score": 0.90,
            "gt_person_present": True,
        },
    )

    assert first_response.status_code == 200
    assert first_response.json()["alert_created"] is False

    assert second_response.status_code == 200
    assert second_response.json()["alert_created"] is False

    assert third_response.status_code == 200
    payload = third_response.json()
    assert payload["accepted"] is True
    assert payload["alert_created"] is True
    assert isinstance(payload["alert_id"], str)


def test_ingest_frame_without_alert_when_score_low() -> None:
    mission_id = client.post("/v1/missions").json()["mission_id"]

    response = client.post(
        "/v1/frames",
        json={
            "mission_id": mission_id,
            "frame_id": 2,
            "ts_sec": 1.0,
            "score": 0.10,
            "gt_person_present": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] is True
    assert payload["alert_created"] is False
    assert payload["alert_id"] is None


def test_alert_cooldown_and_gap_end() -> None:
    mission_id = client.post("/v1/missions").json()["mission_id"]

    responses = [
        client.post(
            "/v1/frames",
            json={
                "mission_id": mission_id,
                "frame_id": 1,
                "ts_sec": 0.0,
                "score": 0.95,
                "gt_person_present": True,
            },
        ),
        client.post(
            "/v1/frames",
            json={
                "mission_id": mission_id,
                "frame_id": 2,
                "ts_sec": 0.3,
                "score": 0.95,
                "gt_person_present": True,
            },
        ),
        client.post(
            "/v1/frames",
            json={
                "mission_id": mission_id,
                "frame_id": 3,
                "ts_sec": 0.6,
                "score": 0.95,
                "gt_person_present": True,
            },
        ),
        client.post(
            "/v1/frames",
            json={
                "mission_id": mission_id,
                "frame_id": 4,
                "ts_sec": 2.0,
                "score": 0.05,
                "gt_person_present": False,
            },
        ),
        client.post(
            "/v1/frames",
            json={
                "mission_id": mission_id,
                "frame_id": 5,
                "ts_sec": 2.1,
                "score": 0.95,
                "gt_person_present": True,
            },
        ),
        client.post(
            "/v1/frames",
            json={
                "mission_id": mission_id,
                "frame_id": 6,
                "ts_sec": 2.3,
                "score": 0.95,
                "gt_person_present": True,
            },
        ),
        client.post(
            "/v1/frames",
            json={
                "mission_id": mission_id,
                "frame_id": 7,
                "ts_sec": 2.5,
                "score": 0.95,
                "gt_person_present": True,
            },
        ),
        client.post(
            "/v1/frames",
            json={
                "mission_id": mission_id,
                "frame_id": 8,
                "ts_sec": 5.8,
                "score": 0.95,
                "gt_person_present": True,
            },
        ),
        client.post(
            "/v1/frames",
            json={
                "mission_id": mission_id,
                "frame_id": 9,
                "ts_sec": 6.0,
                "score": 0.95,
                "gt_person_present": True,
            },
        ),
        client.post(
            "/v1/frames",
            json={
                "mission_id": mission_id,
                "frame_id": 10,
                "ts_sec": 6.2,
                "score": 0.95,
                "gt_person_present": True,
            },
        ),
    ]

    assert responses[0].json()["alert_created"] is False
    assert responses[1].json()["alert_created"] is False
    assert responses[2].json()["alert_created"] is True
    assert responses[3].json()["alert_created"] is False
    assert responses[4].json()["alert_created"] is False
    assert responses[5].json()["alert_created"] is False
    assert responses[6].json()["alert_created"] is False
    assert responses[7].json()["alert_created"] is False
    assert responses[8].json()["alert_created"] is False
    assert responses[9].json()["alert_created"] is True


def test_ingest_frame_mission_not_found() -> None:
    response = client.post(
        "/v1/frames",
        json={
            "mission_id": "not-exists",
            "frame_id": 3,
            "ts_sec": 1.5,
            "score": 0.90,
            "gt_person_present": True,
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Mission not found"


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


def test_mission_episodes() -> None:
    mission_id = client.post("/v1/missions").json()["mission_id"]

    frames = [
        {
            "frame_id": 0,
            "ts_sec": 0.0,
            "score": 0.1,
            "gt_person_present": False,
        },
        {
            "frame_id": 1,
            "ts_sec": 0.5,
            "score": 0.9,
            "gt_person_present": True,
        },
        {
            "frame_id": 2,
            "ts_sec": 1.0,
            "score": 0.9,
            "gt_person_present": True,
        },
        {
            "frame_id": 3,
            "ts_sec": 1.5,
            "score": 0.2,
            "gt_person_present": False,
        },
        {
            "frame_id": 4,
            "ts_sec": 2.0,
            "score": 0.9,
            "gt_person_present": True,
        },
        {
            "frame_id": 5,
            "ts_sec": 2.5,
            "score": 0.2,
            "gt_person_present": False,
        },
    ]

    for frame in frames:
        payload = {"mission_id": mission_id, **frame}
        response = client.post("/v1/frames", json=payload)
        assert response.status_code == 200

    response = client.get(f"/v1/missions/{mission_id}/episodes")

    assert response.status_code == 200
    payload = response.json()
    assert payload["episodes_total"] == 2

    episodes = payload["episodes"]
    assert episodes[0]["start_frame_id"] == 1
    assert episodes[0]["end_frame_id"] == 2
    assert episodes[1]["start_frame_id"] == 4
    assert episodes[1]["end_frame_id"] == 4


def test_mission_episodes_not_found() -> None:
    response = client.get("/v1/missions/not-exists/episodes")

    assert response.status_code == 404
    assert response.json()["detail"] == "Mission not found"
