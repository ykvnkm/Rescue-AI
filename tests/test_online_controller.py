"""Tests for DetectionStreamController behavior."""

from __future__ import annotations

from rescue_ai.config import Settings
from rescue_ai.interfaces.cli.online import DetectionStreamController


class _FakeRpiClient:
    def __init__(self, _settings) -> None:
        self._settings = _settings

    def start_stream(self, mission_id: str, target_fps: float, timeout_sec: float):
        _ = (target_fps, timeout_sec)
        return type(
            "Session",
            (),
            {"session_id": f"s-{mission_id}", "rtsp_url": "rtsp://host/live/s"},
        )()

    def stop_stream(self, session_id: str, timeout_sec: float):
        _ = (session_id, timeout_sec)
        return {"stopped": True}

    def session_stats(self, session_id: str, timeout_sec: float):
        _ = (session_id, timeout_sec)
        return {"processed": 7}

    def health(self, timeout_sec: float):
        _ = timeout_sec
        return {"status": "ok"}

    def catalog(self, timeout_sec: float):
        _ = timeout_sec
        mission = type("Mission", (), {"mission_id": "m-demo", "name": "Demo"})()
        return type("Catalog", (), {"missions": [mission]})()


def _settings() -> Settings:
    from rescue_ai.config import (
        ApiSettings,
        AppSettings,
        BatchSettings,
        DatabaseSettings,
        DetectionSettings,
        RpiSettings,
        StorageSettings,
    )

    return Settings(
        app=AppSettings(),
        api=ApiSettings(),
        database=DatabaseSettings(),
        storage=StorageSettings(),
        rpi=RpiSettings(
            RPI_BASE_URL="http://192.168.0.118:9100",
            RPI_RTSP_PORT=8554,
            RPI_RTSP_PATH_PREFIX="live",
            RPI_TIMEOUT_SEC=1.0,
        ),
        batch=BatchSettings(),
        detection=DetectionSettings(),
    )


def test_detection_stream_controller_lifecycle(monkeypatch) -> None:
    from rescue_ai.interfaces.cli import online as online_main

    monkeypatch.setattr(online_main, "RpiClient", _FakeRpiClient)
    controller = DetectionStreamController(_settings())

    started = controller.start(mission_id="m1", rpi_mission_id="rpi-1", target_fps=6.0)
    assert started.running is True
    assert started.session_id == "s-rpi-1"

    payload = controller.as_payload("m1")
    assert payload is not None
    assert payload["running"] is True
    assert payload["last_stats"] == {"processed": 7}

    stopped = controller.stop("m1")
    assert stopped is not None
    assert stopped.running is False

    assert controller.check_rpi_health()["status"] == "ok"
    assert controller.list_rpi_missions() == [{"mission_id": "m-demo", "name": "Demo"}]
    assert controller.stop("missing") is None
