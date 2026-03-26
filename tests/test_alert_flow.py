"""Pilot alert flow API tests."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from fastapi.testclient import TestClient

from rescue_ai.interfaces.api.app import app
from rescue_ai.interfaces.api.dependencies import (
    get_pilot_service,
    get_stream_controller,
    reset_state,
)

client = TestClient(app)


class _FakeDetector:
    def __init__(self, config: object) -> None:
        self._config = config

    def warmup(self) -> None:
        return None

    def detect(self, _frame_path: str) -> list[object]:
        return []

    def runtime_name(self) -> str:
        return "fake-detector"


def setup_function() -> None:
    os.environ["ARTIFACTS_BACKEND"] = "local"
    reset_state()
    cast(Any, get_stream_controller())._orchestrator.set_detector_factory(
        cast(Any, _FakeDetector)
    )


def _build_mission_source_layout(root: Path) -> Path:
    images_dir = root / "images"
    annotations_dir = root / "annotations"
    images_dir.mkdir()
    annotations_dir.mkdir()

    frame_path = images_dir / "frame_0001.jpg"
    frame_path.write_bytes(b"\xff\xd8\xff\xd9")
    payload = {
        "images": [{"id": 1, "file_name": "frame_0001.jpg"}],
        "categories": [{"id": 1, "name": "person"}],
        "annotations": [],
    }
    (annotations_dir / "mission.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return images_dir


def _create_mission() -> str:
    with TemporaryDirectory() as temp_dir:
        images_dir = _build_mission_source_layout(Path(temp_dir))
        response = client.post(
            "/v1/missions/start-flow",
            json={
                "source_name": "pilot-set",
                "fps": 2.0,
                "frames_dir": str(images_dir),
                "annotations_path": None,
                "api_base": "http://127.0.0.1:1",
            },
        )
    assert response.status_code == 200
    return response.json()["mission_id"]


def _frame_payload(
    mission_id: str,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    frame_id = int(overrides.get("frame_id", 0))
    score = overrides.get("score")
    detections: list[dict[str, Any]] = []
    if score is not None:
        detections.append(
            {
                "bbox": [10.0, 20.0, 50.0, 80.0],
                "score": score,
                "label": "person",
                "model_name": "yolo8n",
                "explanation": "test-detection",
            }
        )

    return {
        "frame_id": frame_id,
        "ts_sec": float(overrides.get("ts_sec", 0.0)),
        "image_uri": overrides.get("image_uri")
        or f"s3://frames/{mission_id}/{frame_id}.jpg",
        "gt_person_present": bool(overrides.get("gt_person_present", False)),
        "gt_episode_id": overrides.get("gt_episode_id"),
        "detections": detections,
    }


def _ingest_frame(mission_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        f"/v1/missions/{mission_id}/frames",
        json=payload,
    )
    assert response.status_code == 200
    return response.json()


def test_mission_complete_flow() -> None:
    mission_id = _create_mission()
    complete_response = client.post(f"/v1/missions/{mission_id}/complete")
    assert complete_response.status_code == 200
    assert complete_response.json()["status"] == "completed"


def test_alert_rule_triggers_on_first_person_detection() -> None:
    mission_id = _create_mission()

    first = _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id=mission_id,
            overrides={
                "frame_id": 1,
                "ts_sec": 0.0,
                "gt_person_present": True,
                "score": 0.95,
            },
        ),
    )
    assert first["alerts_created"] == 1


def test_people_detected_matches_bbox_count() -> None:
    mission_id = _create_mission()

    result = _ingest_frame(
        mission_id,
        {
            "frame_id": 10,
            "ts_sec": 10.0,
            "image_uri": f"s3://frames/{mission_id}/10.jpg",
            "gt_person_present": True,
            "gt_episode_id": None,
            "detections": [
                {
                    "bbox": [10.0, 20.0, 40.0, 80.0],
                    "score": 0.96,
                    "label": "person",
                    "model_name": "yolo8n",
                    "explanation": "person-1",
                },
                {
                    "bbox": [100.0, 120.0, 140.0, 180.0],
                    "score": 0.91,
                    "label": "person",
                    "model_name": "yolo8n",
                    "explanation": "person-2",
                },
            ],
        },
    )

    assert result["alerts_created"] == 1
    alert_id = result["alert_ids"][0]
    details = client.get(f"/v1/alerts/{alert_id}")
    assert details.status_code == 200
    assert details.json()["people_detected"] == 2


def test_alert_rule_applies_cooldown_and_gap_end() -> None:
    mission_id = _create_mission()

    trigger = _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 1, "ts_sec": 0.0, "gt_person_present": True, "score": 0.95},
        ),
    )
    blocked = _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 2, "ts_sec": 0.5, "gt_person_present": True, "score": 0.95},
        ),
    )

    assert trigger["alerts_created"] == 1
    assert blocked["alerts_created"] == 0

    _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 3, "ts_sec": 1.3, "gt_person_present": False, "score": None},
        ),
    )
    after_gap_first = _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 4, "ts_sec": 1.8, "gt_person_present": True, "score": 0.95},
        ),
    )
    after_gap_second = _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 5, "ts_sec": 3.4, "gt_person_present": True, "score": 0.95},
        ),
    )

    assert after_gap_first["alerts_created"] == 1
    assert after_gap_second["alerts_created"] == 1


def test_low_score_detection_not_promoted_to_alert() -> None:
    mission_id = _create_mission()

    result = _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id=mission_id,
            overrides={
                "frame_id": 1,
                "ts_sec": 0.0,
                "gt_person_present": False,
                "score": 0.1,
            },
        ),
    )

    assert result["alerts_created"] == 0
    assert result["alert_ids"] == []


def test_list_alerts_with_status_filter() -> None:
    mission_id = _create_mission()

    ingest_result = _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 1, "ts_sec": 0.0, "gt_person_present": True, "score": 0.95},
        ),
    )
    _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 2, "ts_sec": 0.5, "gt_person_present": True, "score": 0.95},
        ),
    )
    alert_id = ingest_result["alert_ids"][0]

    confirm_response = client.post(
        f"/v1/alerts/{alert_id}/confirm",
        json={
            "reviewed_by": "operator_1",
            "reviewed_at_sec": 0.9,
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
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 1, "ts_sec": 0.0, "gt_person_present": True, "score": 0.95},
        ),
    )
    _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 2, "ts_sec": 0.5, "gt_person_present": True, "score": 0.95},
        ),
    )
    alert_id = ingest_result["alert_ids"][0]

    first_response = client.post(
        f"/v1/alerts/{alert_id}/confirm",
        json={"reviewed_by": "operator_1", "reviewed_at_sec": 0.8},
    )
    second_response = client.post(
        f"/v1/alerts/{alert_id}/reject",
        json={"reviewed_by": "operator_2", "reviewed_at_sec": 1.0},
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 409
    assert second_response.json()["detail"] == "Alert already reviewed"


def test_mission_report_metrics() -> None:
    mission_id = _create_mission()

    first_alert = _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 0, "ts_sec": 0.0, "gt_person_present": True, "score": 0.95},
        ),
    )["alert_ids"][0]
    _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 1, "ts_sec": 0.5, "gt_person_present": True, "score": 0.95},
        ),
    )
    _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 2, "ts_sec": 1.0, "gt_person_present": False, "score": None},
        ),
    )
    second_alert = _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 3, "ts_sec": 3.2, "gt_person_present": True, "score": 0.95},
        ),
    )["alert_ids"][0]
    _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 4, "ts_sec": 3.6, "gt_person_present": True, "score": 0.95},
        ),
    )

    confirm_response = client.post(
        f"/v1/alerts/{first_alert}/confirm",
        json={"reviewed_by": "operator_1", "reviewed_at_sec": 0.9},
    )
    reject_response = client.post(
        f"/v1/alerts/{second_alert}/reject",
        json={"reviewed_by": "operator_1", "reviewed_at_sec": 3.8},
    )
    assert confirm_response.status_code == 200
    assert reject_response.status_code == 200

    report_response = client.get(f"/v1/missions/{mission_id}/report")
    assert report_response.status_code == 200
    report = report_response.json()

    assert report["mission_id"] == mission_id
    assert report["episodes_total"] == 2
    assert report["episodes_found"] == 2
    assert report["recall_event"] == 1.0
    assert report["ttfc_sec"] == 0.9
    assert report["false_alerts_total"] == 0
    assert report["alerts_total"] == 2
    assert report["alerts_confirmed"] == 1
    assert report["alerts_rejected"] == 1
    assert report["config_name"] == "nsu_frames_yolov8n_alert_contract"
    assert isinstance(report["config_hash"], str)
    assert len(report["config_hash"]) == 64
    assert report["config_path"] == "configs/nsu_frames_yolov8n_alert_contract.yaml"
    assert report["model_url"].endswith("yolov8n_baseline_multiscale.pt")
    assert isinstance(report["service_version"], str)


def test_mission_report_ttfc_uses_first_episode_and_tolerance() -> None:
    mission_id = _create_mission()

    _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 0, "ts_sec": 8.0, "gt_person_present": False, "score": None},
        ),
    )
    alert_outside = _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 1, "ts_sec": 8.5, "gt_person_present": False, "score": 0.95},
        ),
    )["alert_ids"][0]

    _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 2, "ts_sec": 10.0, "gt_person_present": True, "score": None},
        ),
    )
    alert_first_episode = _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 3, "ts_sec": 10.9, "gt_person_present": True, "score": 0.95},
        ),
    )["alert_ids"][0]
    _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 4, "ts_sec": 12.3, "gt_person_present": True, "score": 0.95},
        ),
    )
    _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 5, "ts_sec": 19.0, "gt_person_present": False, "score": None},
        ),
    )

    alert_second_episode = _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 6, "ts_sec": 40.0, "gt_person_present": True, "score": 0.95},
        ),
    )["alert_ids"][0]
    _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 7, "ts_sec": 41.0, "gt_person_present": True, "score": 0.95},
        ),
    )
    _ingest_frame(
        mission_id,
        _frame_payload(
            mission_id,
            {"frame_id": 8, "ts_sec": 47.0, "gt_person_present": False, "score": None},
        ),
    )

    assert (
        client.post(
            f"/v1/alerts/{alert_outside}/confirm",
            json={"reviewed_by": "operator_1", "reviewed_at_sec": 9.0},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/alerts/{alert_first_episode}/confirm",
            json={"reviewed_by": "operator_1", "reviewed_at_sec": 14.8},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/alerts/{alert_second_episode}/confirm",
            json={"reviewed_by": "operator_1", "reviewed_at_sec": 43.0},
        ).status_code
        == 200
    )

    report_response = client.get(f"/v1/missions/{mission_id}/report")
    assert report_response.status_code == 200
    report = report_response.json()

    assert report["ttfc_sec"] == 4.8


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


def test_episode_debug_endpoint_returns_timeline() -> None:
    mission_id = _create_mission()
    response = client.get(f"/v1/missions/{mission_id}/debug/episodes?limit=10")
    assert response.status_code == 200
    payload = response.json()
    assert payload["mission_id"] == mission_id
    assert payload["rows_limit"] == 10
    assert "episodes_total" in payload
    assert isinstance(payload["rows"], list)


def test_report_uses_only_frames_before_manual_completion_cutoff() -> None:
    service = get_pilot_service()
    mission = service.create_mission(source_name="pilot-set", total_frames=0, fps=2.0)
    service.start_mission(mission.mission_id)

    _ingest_frame(
        mission.mission_id,
        _frame_payload(
            mission.mission_id,
            {"frame_id": 0, "ts_sec": 0.0, "gt_person_present": True, "score": 0.95},
        ),
    )
    _ingest_frame(
        mission.mission_id,
        _frame_payload(
            mission.mission_id,
            {"frame_id": 1, "ts_sec": 0.5, "gt_person_present": True, "score": 0.95},
        ),
    )

    completed = service.complete_mission(
        mission_id=mission.mission_id,
        completed_frame_id=1,
    )
    assert completed is not None
    assert completed.completed_frame_id == 1

    _ingest_frame(
        mission.mission_id,
        _frame_payload(
            mission.mission_id,
            {"frame_id": 2, "ts_sec": 1.0, "gt_person_present": False, "score": None},
        ),
    )
    _ingest_frame(
        mission.mission_id,
        _frame_payload(
            mission.mission_id,
            {"frame_id": 3, "ts_sec": 1.5, "gt_person_present": False, "score": None},
        ),
    )

    report = service.get_mission_report(mission.mission_id)
    assert report["episodes_total"] == 1
    assert report["alerts_total"] == 1


def test_alert_frame_endpoint_returns_image() -> None:
    mission_id = _create_mission()
    with TemporaryDirectory() as temp_dir:
        frame_path = Path(temp_dir) / "frame_0001.jpg"
        frame_path.write_bytes(b"\xff\xd8\xff\xd9")

        ingest_result = _ingest_frame(
            mission_id,
            _frame_payload(
                mission_id=mission_id,
                overrides={
                    "frame_id": 1,
                    "ts_sec": 0.0,
                    "gt_person_present": True,
                    "score": 0.95,
                    "image_uri": str(frame_path),
                },
            ),
        )
        alert_id = ingest_result["alert_ids"][0]

        image_response = client.get(f"/v1/alerts/{alert_id}/frame")
        assert image_response.status_code == 200
        assert image_response.headers["content-type"].startswith("image/jpeg")


def test_stream_status_defaults_to_not_running() -> None:
    with TemporaryDirectory() as temp_dir:
        images_dir = _build_mission_source_layout(Path(temp_dir))
        response = client.post(
            "/v1/missions/start-flow",
            json={
                "source_name": "pilot-set",
                "fps": 2.0,
                "frames_dir": str(images_dir),
                "annotations_path": None,
                "api_base": "http://127.0.0.1:1",
            },
        )
    assert response.status_code == 200
    mission_id = response.json()["mission_id"]
    status_response = client.get(f"/v1/missions/{mission_id}/stream/status")
    assert status_response.status_code == 200
    payload = status_response.json()
    assert payload["mission_id"] == mission_id
    assert payload["processed_frames"] >= 0
    assert payload["total_frames"] >= 1


def test_stream_status_missing_mission_returns_404() -> None:
    response = client.get("/v1/missions/not-exists/stream/status")
    assert response.status_code == 404
    assert response.json()["detail"] == "Mission not found"


def test_start_flow_returns_400_for_missing_directory() -> None:
    response = client.post(
        "/v1/missions/start-flow",
        json={
            "source_name": "pilot-set",
            "fps": 2.0,
            "frames_dir": "/path/not/found",
            "annotations_path": None,
            "api_base": "http://127.0.0.1:8000",
        },
    )
    assert response.status_code == 400
    assert "frames dir not found" in response.json()["detail"]


def test_start_flow_creates_and_starts_mission() -> None:
    with TemporaryDirectory() as temp_dir:
        images_dir = _build_mission_source_layout(Path(temp_dir))
        response = client.post(
            "/v1/missions/start-flow",
            json={
                "source_name": "pilot-set",
                "fps": 2.0,
                "frames_dir": str(images_dir),
                "annotations_path": None,
                "api_base": "http://127.0.0.1:1",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mission_id"]
    assert payload["status"] == "running"
    assert payload["total_frames"] == 1


def test_start_flow_returns_503_when_detector_preflight_fails() -> None:
    class _FailingDetector:
        def __init__(self, config: object) -> None:
            self._config = config

        def warmup(self) -> None:
            raise RuntimeError(
                "model cache is empty and remote download is unavailable"
            )

        def predict(self, _frame_path: Path) -> list[object]:
            return []

    cast(Any, get_stream_controller())._orchestrator.set_detector_factory(
        cast(Any, _FailingDetector)
    )

    with TemporaryDirectory() as temp_dir:
        images_dir = _build_mission_source_layout(Path(temp_dir))
        response = client.post(
            "/v1/missions/start-flow",
            json={
                "source_name": "pilot-set",
                "fps": 2.0,
                "frames_dir": str(images_dir),
                "annotations_path": None,
                "api_base": "http://127.0.0.1:1",
            },
        )

    assert response.status_code == 503
    assert "Mission preflight failed" in response.json()["detail"]
