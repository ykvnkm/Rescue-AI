"""Tests for the current public REST API surface."""

from __future__ import annotations

from fastapi.testclient import TestClient

from rescue_ai.interfaces.api.app import app

client = TestClient(app)


class _FakeMission:
    def __init__(
        self, mission_id: str, status: str, source_name: str, fps: float
    ) -> None:
        self.mission_id = mission_id
        self.status = status
        self.source_name = source_name
        self.fps = fps
        self.completed_frame_id = None


class _FakePilotService:
    def __init__(self) -> None:
        self._mission = _FakeMission("m-1", "created", "rpi:demo", 6.0)

    def create_mission(self, source_name: str, total_frames: int, fps: float):
        _ = total_frames
        self._mission = _FakeMission("m-1", "created", source_name, fps)
        return self._mission

    def start_mission(self, mission_id: str):
        if mission_id != self._mission.mission_id:
            return None
        self._mission.status = "running"
        return self._mission

    def get_mission(self, mission_id: str):
        if mission_id != self._mission.mission_id:
            return None
        return self._mission

    def complete_mission(self, mission_id: str, completed_frame_id=None):
        _ = completed_frame_id
        if mission_id != self._mission.mission_id:
            return None
        self._mission.status = "completed"
        return self._mission

    def get_mission_report(self, mission_id: str):
        if mission_id != self._mission.mission_id:
            raise ValueError("Mission not found")
        return {"mission_id": mission_id, "status": "completed"}

    def ingest_frame_event(self, frame_event, detections):
        _ = (frame_event, detections)
        return []


class _FakeStreamController:
    def check_rpi_health(self) -> dict[str, object]:
        return {"status": "ok"}

    def list_rpi_missions(self) -> list[dict[str, str]]:
        return [
            {"mission_id": "demo-rpi-mission", "name": "Demo mission"},
            {"mission_id": "forest-2026-03-29", "name": "Forest 2026-03-29"},
        ]

    def start(self, *, mission_id: str, rpi_mission_id: str, target_fps: float):
        _ = (mission_id, rpi_mission_id, target_fps)
        return type("Session", (), {"session_id": "s1"})()

    def as_payload(self, mission_id: str):
        return {"mission_id": mission_id, "running": True}

    def stop(self, mission_id: str):
        _ = mission_id
        return {"running": False}


def test_health_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ready_not_ready_without_env(monkeypatch) -> None:
    from rescue_ai.config import (
        ApiSettings,
        AppSettings,
        BatchSettings,
        DatabaseSettings,
        DetectionSettings,
        RpiSettings,
        Settings,
        StorageSettings,
    )
    from rescue_ai.interfaces.api import routes

    monkeypatch.setattr(
        routes,
        "get_settings",
        lambda: Settings(
            app=AppSettings(),
            api=ApiSettings(),
            database=DatabaseSettings(DB_DSN=""),
            storage=StorageSettings(
                ARTIFACTS_S3_BUCKET="",
                ARTIFACTS_S3_ACCESS_KEY_ID="",
                ARTIFACTS_S3_SECRET_ACCESS_KEY="",
            ),
            rpi=RpiSettings(RPI_BASE_URL=""),
            batch=BatchSettings(),
            detection=DetectionSettings(),
        ),
    )
    response = client.get("/ready")
    assert response.status_code == 503


def test_predict_flow_smoke(monkeypatch) -> None:
    from rescue_ai.interfaces.api import routes

    pilot = _FakePilotService()
    stream = _FakeStreamController()
    monkeypatch.setattr(routes, "get_pilot_service", lambda: pilot)
    monkeypatch.setattr(routes, "get_stream_controller", lambda: stream)

    start = client.post(
        "/predict/start",
        json={"rpi_mission_id": "demo-rpi-mission"},
    )
    assert start.status_code == 200
    assert start.json()["mission_id"] == "m-1"
    assert start.json()["status"] == "running"

    status = client.get("/predict/m-1")
    assert status.status_code == 200
    assert status.json()["mission_id"] == "m-1"

    stop = client.post("/predict/m-1/stop")
    assert stop.status_code == 200
    assert stop.json()["status"] == "completed"


def test_rpi_missions_returns_catalog(monkeypatch) -> None:
    from rescue_ai.interfaces.api import routes

    def _stream_controller_factory() -> _FakeStreamController:
        return _FakeStreamController()

    monkeypatch.setattr(routes, "get_stream_controller", _stream_controller_factory)

    response = client.get("/rpi/missions")
    assert response.status_code == 200
    data = response.json()
    assert [item["mission_id"] for item in data["missions"]] == [
        "demo-rpi-mission",
        "forest-2026-03-29",
    ]
