"""Mission and alert flow API tests."""

from fastapi.testclient import TestClient

from services.api_gateway.app import app
from services.api_gateway.infrastructure import memory_store

client = TestClient(app)


def setup_function() -> None:
    memory_store.reset_state()


def _post_frame(
    mission_id: str,
    frame_id: int,
    ts_sec: float,
    score: float,
    gt_person_present: bool,
) -> dict[str, object]:
    response = client.post(
        "/v1/frames",
        json={
            "mission_id": mission_id,
            "frame_id": frame_id,
            "ts_sec": round(ts_sec, 3),
            "score": score,
            "gt_person_present": gt_person_present,
        },
    )
    assert response.status_code == 200
    return response.json()


def _ingest_hits(
    mission_id: str,
    start_frame_id: int,
    start_ts: float,
    count: int,
    step: float,
) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for index in range(count):
        payloads.append(
            _post_frame(
                mission_id=mission_id,
                frame_id=start_frame_id + index,
                ts_sec=start_ts + index * step,
                score=0.95,
                gt_person_present=True,
            )
        )
    return payloads


def test_create_mission() -> None:
    response = client.post("/v1/missions")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "created"
    assert isinstance(payload["mission_id"], str)
    assert payload["mission_id"]


def test_ingest_frame_creates_alert_after_quorum() -> None:
    mission_id = client.post("/v1/missions").json()["mission_id"]
    frame_step = max(memory_store.ALERT_WINDOW_SEC / 4, 0.05)
    responses = _ingest_hits(
        mission_id=mission_id,
        start_frame_id=1,
        start_ts=0.0,
        count=memory_store.ALERT_QUORUM + 1,
        step=frame_step,
    )

    for payload in responses[: max(memory_store.ALERT_QUORUM - 1, 0)]:
        assert payload["alert_created"] is False

    assert responses[memory_store.ALERT_QUORUM - 1]["alert_created"] is True
    assert responses[memory_store.ALERT_QUORUM]["alert_created"] is False
    assert responses[memory_store.ALERT_QUORUM]["alert_id"] is None


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
    frame_step = max(memory_store.ALERT_WINDOW_SEC / 4, 0.05)
    near_gap = max(memory_store.ALERT_GAP_END_SEC + 0.1, 0.2)
    first_batch = _ingest_hits(
        mission_id=mission_id,
        start_frame_id=1,
        start_ts=0.0,
        count=memory_store.ALERT_QUORUM,
        step=frame_step,
    )
    frame_id = memory_store.ALERT_QUORUM + 1

    first_alert_ts = round((memory_store.ALERT_QUORUM - 1) * frame_step, 3)

    for payload in first_batch[: max(memory_store.ALERT_QUORUM - 1, 0)]:
        assert payload["alert_created"] is False
    assert first_batch[memory_store.ALERT_QUORUM - 1]["alert_created"] is True

    gap_break_response = _post_frame(
        mission_id=mission_id,
        frame_id=frame_id,
        ts_sec=first_alert_ts + near_gap,
        score=0.01,
        gt_person_present=False,
    )
    assert gap_break_response["alert_created"] is False
    frame_id += 1

    early_margin = (memory_store.ALERT_QUORUM * frame_step) + 0.05
    early_start_ts = first_alert_ts + memory_store.ALERT_COOLDOWN_SEC - early_margin
    early_responses = _ingest_hits(
        mission_id=mission_id,
        start_frame_id=frame_id,
        start_ts=early_start_ts,
        count=memory_store.ALERT_QUORUM,
        step=frame_step,
    )
    frame_id += memory_store.ALERT_QUORUM

    for payload in early_responses:
        assert payload["alert_created"] is False

    early_end_ts = early_start_ts + (memory_store.ALERT_QUORUM - 1) * frame_step
    second_start_ts = max(
        first_alert_ts + memory_store.ALERT_COOLDOWN_SEC + 0.2,
        early_end_ts + memory_store.ALERT_WINDOW_SEC + 0.1,
    )
    second_responses = _ingest_hits(
        mission_id=mission_id,
        start_frame_id=frame_id,
        start_ts=second_start_ts,
        count=memory_store.ALERT_QUORUM,
        step=frame_step,
    )

    for payload in second_responses[: max(memory_store.ALERT_QUORUM - 1, 0)]:
        assert payload["alert_created"] is False
    assert second_responses[memory_store.ALERT_QUORUM - 1]["alert_created"] is True


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


def test_get_alert_details() -> None:
    mission_id = client.post("/v1/missions").json()["mission_id"]
    alert = memory_store.add_alert(
        mission_id=mission_id,
        frame_id=10,
        ts_sec=1.1,
        score=0.91,
    )

    response = client.get(f"/v1/alerts/{alert.alert_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["alert_id"] == alert.alert_id
    assert payload["mission_id"] == mission_id
    assert payload["status"] == "queued"


def test_get_alert_details_not_found() -> None:
    response = client.get("/v1/alerts/not-exists")

    assert response.status_code == 404
    assert response.json()["detail"] == "Alert not found"


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


def test_review_processed_alert_returns_409() -> None:
    mission_id = client.post("/v1/missions").json()["mission_id"]
    alert = memory_store.add_alert(
        mission_id=mission_id,
        frame_id=1,
        ts_sec=0.5,
        score=0.95,
    )

    first_response = client.post(
        f"/v1/alerts/{alert.alert_id}/confirm",
        json={"reviewed_by": "operator_1"},
    )
    second_response = client.post(
        f"/v1/alerts/{alert.alert_id}/reject",
        json={"reviewed_by": "operator_2"},
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 409
    assert second_response.json()["detail"] == "Alert already reviewed"


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
