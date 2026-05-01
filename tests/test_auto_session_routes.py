"""Route tests for automatic-session API wiring."""

import asyncio
from types import SimpleNamespace
from typing import cast

from fastapi import Request

from rescue_ai.domain.value_objects import NavMode
from rescue_ai.interfaces.api import routes_auto_sessions


class _FakeManager:
    def __init__(self) -> None:
        self.build_source_calls: list[dict[str, object]] = []
        self.start_request = None

    def build_source(
        self,
        *,
        source_kind: str,
        source_value: str,
        fps: float | None,
        rpi_mission_id: str = "",
        demo_loop: bool = False,
    ):
        # Accept the route's full keyword shape (rpi_mission_id /
        # demo_loop come from the stream-mode + UI toggles); only the
        # canonical inputs are recorded for the fps-override assertion.
        _ = (rpi_mission_id, demo_loop)
        self.build_source_calls.append(
            {"source_kind": source_kind, "source_value": source_value, "fps": fps}
        )
        return object(), source_value, 30.0

    def start_session(self, *, request):
        self.start_request = request
        return _FakeSession(request)


class _FakeSession:
    def __init__(self, request) -> None:
        self._request = request
        self.session_id = "session-1"
        self.mission = SimpleNamespace(mission_id="mission-1")

    def info(self):
        return SimpleNamespace(
            session_id="session-1",
            mission_id="mission-1",
            source_kind=self._request.source_kind,
            source_value=self._request.source_value,
            nav_mode=str(self._request.nav_mode),
            detector_name=self._request.detector_name,
            fps=self._request.fps,
            started_at="2026-04-28T13:00:00+00:00",
        )


def test_video_session_ignores_form_fps_override(monkeypatch, tmp_path) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(routes_auto_sessions, "_require_manager", lambda: manager)

    # The route validates the local file exists before calling
    # build_source — supply a real path so we exercise the full happy
    # path and only stub out the manager.
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"x")

    class _FakeRequest:
        async def form(self):
            return {
                "source_kind": "video",
                "source_value": str(video_path),
                "nav_mode": NavMode.MARKER.value,
                "detector_name": "yolo",
                "fps": "3.0",
                "nsu_channel": "local",
                "rpi_mission_id": "",
                "detect_enabled": "true",
                "save_video": "false",
                "demo_loop": "false",
                "file": None,
            }

    response = asyncio.run(
        routes_auto_sessions.start_auto_session(cast(Request, _FakeRequest()))
    )

    assert manager.build_source_calls == [
        {"source_kind": "video", "source_value": str(video_path), "fps": None}
    ]
    assert manager.start_request is not None
    assert manager.start_request.fps == 30.0
    assert response["fps"] == 30.0
