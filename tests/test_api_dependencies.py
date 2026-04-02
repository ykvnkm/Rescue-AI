"""Tests for API dependency runtime container."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from rescue_ai.application.pilot_service import PilotService
from rescue_ai.interfaces.api import dependencies


class _FakePilotService:
    def __init__(self) -> None:
        self.reset_called = False

    def reset_runtime_state(self) -> None:
        self.reset_called = True


class _FakeStreamController:
    def start(
        self,
        *,
        mission_id: str,
        rpi_mission_id: str,
        target_fps: float,
    ) -> object:
        _ = (mission_id, rpi_mission_id, target_fps)
        return {"started": True}

    def stop(self, mission_id: str) -> object | None:
        _ = mission_id
        return {"stopped": True}

    def as_payload(self, mission_id: str) -> dict[str, object] | None:
        _ = mission_id
        return {"running": False}

    def check_rpi_health(self) -> dict[str, object]:
        return {"status": "ok"}

    def list_rpi_missions(self) -> list[dict[str, str]]:
        return [{"mission_id": "demo", "name": "Demo"}]


def test_lazy_runtime_bootstrap_and_getters(monkeypatch) -> None:
    dependencies._STATE.runtime = None

    pilot = _FakePilotService()
    stream = _FakeStreamController()

    module = SimpleNamespace(build_api_runtime=lambda: (pilot, stream, lambda: None, None))
    monkeypatch.setattr(dependencies.importlib, "import_module", lambda _: module)

    assert dependencies.get_container().pilot_service is pilot
    assert dependencies.get_pilot_service() is pilot
    assert dependencies.get_stream_controller() is stream


def test_reset_state_calls_hooks() -> None:
    dependencies._STATE.runtime = None
    pilot = _FakePilotService()
    called = {"reset_hook": False}

    runtime = dependencies.ApiRuntime(
        pilot_service=cast(PilotService, pilot),
        stream_controller=_FakeStreamController(),
        reset_hook=lambda: called.__setitem__("reset_hook", True),
    )
    dependencies.set_runtime(runtime)

    dependencies.reset_state()

    assert called["reset_hook"] is True
    assert pilot.reset_called is True
