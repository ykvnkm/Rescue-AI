"""Pilot alert flow API tests."""

from typing import Any

from fastapi.testclient import TestClient

from services.api_gateway.app import app
from services.api_gateway.dependencies import reset_state

client = TestClient(app)


def setup_function() -> None:
    reset_state()


def _create_mission() -> str:
    response = client.post(
        "/v1/missions",
        json={
            "source_name": "pilot-set",
            "total_frames": 4,
            "fps": 2.0,
        },
    )
    assert response.status_code == 200
    return response.json()["mission_id"]


def _ingest_frame(
    mission_id: str,
    frame_id: int,
    ts_sec: float,
    gt_person_present: bool,
    detections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    response = client.post(
        f"/v1/missions/{mission_id}/frames",
        json={
            "frame_id": frame_id,
            "ts_sec": ts_sec,
            "image_uri": f"s3://frames/{mission_id}/{frame_id}.jpg",
            "gt_person_present": gt_person_present,
            "gt_episode_id": None,
            "detections": detections or [],
        },
    )
    assert response.status_code == 200
    return response.json()


def test_create_mission_and_status_flow() -> None:
    mission_id = _create_mission()

    start_response = client.post(f"/v1/missions/{mission_id}/start")
    complete_response = client.post(f"/v1/missions/{mission_id}/complete")

    assert start_response.status_code == 200
    assert start_response.json()["status"] == "running"
    assert complete_response.status_code == 200
    assert complete_response.json()["status"] == "completed"


def test_ingest_frame_creates_alert_with_bbox() -> None:
    mission_id = _create_mission()

    result = _ingest_frame(
        mission_id=mission_id,
        frame_id=1,
        ts_sec=0.5,
        gt_person_present=True,
        detections=[
            {
                "bbox": [10.0, 20.0, 50.0, 80.0],
                "score": 0.91,
                "label": "person",
                "model_name": "yolo8n",
            }
        ],
    )

    assert result["alerts_created"] == 1
    alert_id = result["alert_ids"][0]

    details = client.get(f"/v1/alerts/{alert_id}")
    assert details.status_code == 200
    payload = details.json()
    assert payload["bbox"] == [10.0, 20.0, 50.0, 80.0]
    assert payload["status"] == "queued"


def test_low_score_detection_not_promoted_to_alert() -> None:
    mission_id = _create_mission()

    result = _ingest_frame(
        mission_id=mission_id,
        frame_id=1,
        ts_sec=0.0,
        gt_person_present=False,
        detections=[
            {
                "bbox": [1.0, 1.0, 2.0, 2.0],
                "score": 0.1,
            }
        ],
    )

    assert result["alerts_created"] == 0
    assert result["alert_ids"] == []


def test_list_alerts_with_status_filter() -> None:
    mission_id = _create_mission()

    ingest_result = _ingest_frame(
        mission_id=mission_id,
        frame_id=1,
        ts_sec=0.0,
        gt_person_present=True,
        detections=[{"bbox": [10, 10, 20, 20], "score": 0.95}],
    )
    alert_id = ingest_result["alert_ids"][0]

    confirm_response = client.post(
        f"/v1/alerts/{alert_id}/confirm",
        json={
            "reviewed_by": "operator_1",
            "reviewed_at_sec": 0.4,
            "decision_reason": "valid target",
        },
    )
    assert confirm_response.status_code == 200

    confirmed = client.get(
        f"/v1/alerts?mission_id={mission_id}&status=reviewed_confirmed"
    )
    queued = client.get(f"/v1/alerts?mission_id={mission_id}&status=queued")

    assert confirmed.status_code == 200
    assert len(confirmed.json()) == 1
    assert queued.status_code == 200
    assert queued.json() == []


def test_review_processed_alert_returns_409() -> None:
    mission_id = _create_mission()
    ingest_result = _ingest_frame(
        mission_id=mission_id,
        frame_id=1,
        ts_sec=0.0,
        gt_person_present=True,
        detections=[{"bbox": [10, 10, 20, 20], "score": 0.95}],
    )
    alert_id = ingest_result["alert_ids"][0]

    first_response = client.post(
        f"/v1/alerts/{alert_id}/confirm",
        json={"reviewed_by": "operator_1", "reviewed_at_sec": 0.5},
    )
    second_response = client.post(
        f"/v1/alerts/{alert_id}/reject",
        json={"reviewed_by": "operator_2", "reviewed_at_sec": 0.7},
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 409
    assert second_response.json()["detail"] == "Alert already reviewed"


def test_mission_report_metrics() -> None:
    mission_id = _create_mission()

    first_alert = _ingest_frame(
        mission_id=mission_id,
        frame_id=0,
        ts_sec=0.0,
        gt_person_present=True,
        detections=[{"bbox": [10, 10, 20, 20], "score": 0.95}],
    )["alert_ids"][0]
    _ingest_frame(
        mission_id=mission_id,
        frame_id=1,
        ts_sec=1.0,
        gt_person_present=False,
    )
    second_alert = _ingest_frame(
        mission_id=mission_id,
        frame_id=2,
        ts_sec=2.0,
        gt_person_present=True,
        detections=[{"bbox": [30, 30, 50, 50], "score": 0.96}],
    )["alert_ids"][0]
    _ingest_frame(
        mission_id=mission_id,
        frame_id=3,
        ts_sec=3.0,
        gt_person_present=False,
    )

    confirm_response = client.post(
        f"/v1/alerts/{first_alert}/confirm",
        json={"reviewed_by": "operator_1", "reviewed_at_sec": 0.8},
    )
    reject_response = client.post(
        f"/v1/alerts/{second_alert}/reject",
        json={"reviewed_by": "operator_1", "reviewed_at_sec": 2.5},
    )
    assert confirm_response.status_code == 200
    assert reject_response.status_code == 200

    report_response = client.get(f"/v1/missions/{mission_id}/report")
    assert report_response.status_code == 200
    report = report_response.json()

    assert report["mission_id"] == mission_id
    assert report["episodes_total"] == 2
    assert report["episodes_found"] == 1
    assert report["recall_event"] == 0.5
    assert report["ttfc_sec"] == 0.8
    assert report["false_alerts_total"] == 1
    assert report["alerts_total"] == 2
    assert report["alerts_confirmed"] == 1
    assert report["alerts_rejected"] == 1


def test_mission_not_found_returns_404() -> None:
    response = client.post(
        "/v1/missions/not-exists/frames",
        json={
            "frame_id": 0,
            "ts_sec": 0.0,
            "image_uri": "s3://frames/missing/0.jpg",
            "gt_person_present": False,
            "gt_episode_id": None,
            "detections": [],
        },
    )
    assert response.status_code == 404


def test_report_not_found_returns_404() -> None:
    response = client.get("/v1/missions/not-exists/report")
    assert response.status_code == 404
    assert response.json()["detail"] == "Mission not found"
