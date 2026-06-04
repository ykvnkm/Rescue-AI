"""Tests for /auto-missions/* API routes (ADR-0006)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from rescue_ai.application.auto_mission_service import AutoFrameOutcome
from rescue_ai.domain.entities import Alert, AutoDecision, Detection, TrajectoryPoint
from rescue_ai.domain.value_objects import (
    AutoDecisionKind,
    MissionMode,
    TrajectorySource,
)
from rescue_ai.interfaces.api import routes_auto
from rescue_ai.interfaces.api.app import app


@dataclass
class _Mission:
    mission_id: str
    source_name: str
    status: str
    created_at: str
    fps: float
    mode: MissionMode = MissionMode.AUTOMATIC
    completed_frame_id: int | None = None


class _FakeAutoService:
    def __init__(self) -> None:
        self.mission: _Mission | None = None
        self.outcome_queue: list[AutoFrameOutcome] = []
        self.ingest_calls: list[dict[str, Any]] = []
        self.report: dict[str, object] = {"mission_id": "fake", "mode": "automatic"}
        self.complete_called: int = 0

    def start_auto_mission(
        self,
        source_name: str,
        total_frames: int,
        fps: float,
        nav_mode: Any,
        detector_name: str,
        config_json: Any = None,
    ) -> _Mission:
        _ = (total_frames, nav_mode, detector_name, config_json)
        self.mission = _Mission(
            mission_id="auto-mission-1",
            source_name=source_name,
            status="running",
            created_at="2026-04-22T10:00:00+00:00",
            fps=fps,
        )
        return self.mission

    def ingest_frame(
        self,
        mission_id: str,
        frame_bgr: Any,
        ts_sec: float,
        frame_id: int,
        image_uri: str,
    ) -> AutoFrameOutcome:
        self.ingest_calls.append(
            {
                "mission_id": mission_id,
                "ts_sec": ts_sec,
                "frame_id": frame_id,
                "image_uri": image_uri,
                "frame_is_ndarray": isinstance(frame_bgr, np.ndarray),
            }
        )
        if self.outcome_queue:
            return self.outcome_queue.pop(0)
        return AutoFrameOutcome(
            detections=[], trajectory_point=None, alerts=[], decisions=[]
        )

    def complete_auto_mission(
        self, mission_id: str, completed_frame_id: int | None = None
    ) -> _Mission | None:
        _ = (mission_id, completed_frame_id)
        self.complete_called += 1
        if self.mission is None:
            return None
        self.mission.status = "completed"
        self.mission.completed_frame_id = completed_frame_id
        return self.mission

    def get_auto_mission_report(self, mission_id: str) -> dict[str, object]:
        _ = mission_id
        return dict(self.report)


@pytest.fixture(autouse=True)
def _fake_auto_service(monkeypatch):
    service = _FakeAutoService()
    monkeypatch.setattr(routes_auto, "get_auto_mission_service", lambda: service)
    yield service


def _jpeg_bytes() -> bytes:
    try:
        import cv2
    except ImportError:
        pytest.skip("OpenCV not available")
    frame = np.zeros((16, 16, 3), dtype=np.uint8)
    ok, buffer = cv2.imencode(".jpg", frame)
    assert ok
    return buffer.tobytes()


def test_start_auto_mission_returns_running_mission() -> None:
    client = TestClient(app)
    response = client.post(
        "/auto-missions/start",
        json={
            "source_name": "file:///tmp/video.mp4",
            "nav_mode": "marker",
            "detector_name": "nanodet",
            "fps": 4.0,
            "config_json": {"k": "v"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mission_id"] == "auto-mission-1"
    assert body["mode"] == "automatic"
    assert body["status"] == "running"
    assert body["fps"] == 4.0


def test_start_auto_mission_503_when_service_missing(monkeypatch) -> None:
    monkeypatch.setattr(routes_auto, "get_auto_mission_service", lambda: None)
    client = TestClient(app)
    response = client.post(
        "/auto-missions/start",
        json={"source_name": "file:///tmp/v.mp4"},
    )
    assert response.status_code == 503


def test_ingest_frame_decodes_jpeg_and_returns_outcome(_fake_auto_service) -> None:
    detection = Detection(
        bbox=(1.0, 2.0, 3.0, 4.0),
        score=0.91,
        label="person",
        model_name="yolo",
    )
    alert = Alert(
        alert_id="a-1",
        mission_id="auto-mission-1",
        frame_id=7,
        ts_sec=1.5,
        image_uri="s3://bucket/k",
        people_detected=1,
        primary_detection=detection,
        detections=[detection],
    )
    decision = AutoDecision(
        decision_id="d-1",
        mission_id="auto-mission-1",
        frame_id=7,
        ts_sec=1.5,
        kind=AutoDecisionKind.ALERT_CREATED,
        reason="quorum",
        created_at="2026-04-22T10:00:01+00:00",
    )
    point = TrajectoryPoint(
        mission_id="auto-mission-1",
        seq=1,
        ts_sec=1.5,
        frame_id=7,
        x=0.1,
        y=0.2,
        z=0.0,
        source=TrajectorySource.MARKER,
    )
    _fake_auto_service.outcome_queue.append(
        AutoFrameOutcome(
            detections=[detection],
            trajectory_point=point,
            alerts=[alert],
            decisions=[decision],
        )
    )

    client = TestClient(app)
    response = client.post(
        "/auto-missions/auto-mission-1/ingest",
        data={"frame_id": "7", "ts_sec": "1.5"},
        files={"image": ("frame.jpg", _jpeg_bytes(), "image/jpeg")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mission_id"] == "auto-mission-1"
    assert body["frame_id"] == 7
    assert body["trajectory_point"]["seq"] == 1
    assert body["alerts"][0]["alert_id"] == "a-1"
    assert body["decisions"][0]["kind"] == "alert_created"

    assert _fake_auto_service.ingest_calls[0]["frame_is_ndarray"] is True


def test_ingest_frame_returns_415_for_non_image_payload() -> None:
    client = TestClient(app)
    response = client.post(
        "/auto-missions/auto-mission-1/ingest",
        data={"frame_id": "1", "ts_sec": "0.0"},
        files={"image": ("note.txt", b"not-an-image", "text/plain")},
    )
    assert response.status_code == 415


def test_complete_auto_mission_returns_report(_fake_auto_service) -> None:
    _fake_auto_service.start_auto_mission(
        source_name="src",
        total_frames=0,
        fps=1.0,
        nav_mode="auto",
        detector_name="yolo",
    )
    client = TestClient(app)
    response = client.post("/auto-missions/auto-mission-1/complete")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["report"]["mode"] == "automatic"


def test_complete_auto_mission_404_when_unknown(_fake_auto_service) -> None:
    _fake_auto_service.mission = None
    client = TestClient(app)
    response = client.post("/auto-missions/missing/complete")
    assert response.status_code == 404


def test_get_auto_mission_report_returns_cached(_fake_auto_service) -> None:
    _fake_auto_service.report = {"mission_id": "x", "foo": "bar"}
    client = TestClient(app)
    response = client.get("/auto-missions/x/report")
    assert response.status_code == 200
    assert response.json()["foo"] == "bar"
