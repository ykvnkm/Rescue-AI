import asyncio
from types import SimpleNamespace

from rescue_ai.domain.value_objects import NavMode
from rescue_ai.interfaces.api import routes_auto_sessions


class _FakeManager:
    def __init__(self) -> None:
        self.build_source_calls = []
        self.start_request = None

    def build_source(self, *, source_kind: str, source_value: str, fps: float | None):
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


def test_video_session_ignores_form_fps_override(monkeypatch) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(routes_auto_sessions, "_require_manager", lambda: manager)

    response = asyncio.run(
        routes_auto_sessions.start_auto_session(
            source_kind="video",
            source_value="D:/video.mp4",
            nav_mode=NavMode.MARKER,
            detector_name="yolo",
            fps=3.0,
            file=None,
        )
    )

    assert manager.build_source_calls == [
        {"source_kind": "video", "source_value": "D:/video.mp4", "fps": None}
    ]
    assert manager.start_request.fps == 30.0
    assert response["fps"] == 30.0
