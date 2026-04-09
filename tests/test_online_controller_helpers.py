"""Focused tests for DetectionStreamController helper logic."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import cast

import numpy as np
import pytest

from rescue_ai.application.pilot_service import PilotService
from rescue_ai.config import (
    ApiSettings,
    AppSettings,
    DatabaseSettings,
    DetectionSettings,
    RpiSettings,
    Settings,
    StorageSettings,
)
from rescue_ai.domain.entities import Detection
from rescue_ai.domain.value_objects import AlertRuleConfig
from rescue_ai.interfaces.cli import online as online_main
from tests.support.in_memory_repositories import (
    InMemoryAlertRepository,
    InMemoryArtifactStorage,
    InMemoryDatabase,
    InMemoryFrameEventRepository,
    InMemoryMissionRepository,
)


class _FakeCapture(online_main._FrameCapture):
    def __init__(self, frames: list[object | None]) -> None:
        self._frames = frames
        self.released = False

    def read_frame(self) -> object | None:
        if not self._frames:
            return None
        return self._frames.pop(0)

    def is_open(self) -> bool:
        return True

    def release(self) -> None:
        self.released = True


class _FakePilotService:
    def __init__(self) -> None:
        self.raise_on_ingest = False

    def ingest_frame_event(self, frame_event, detections):
        _ = frame_event
        if self.raise_on_ingest:
            raise RuntimeError("ingest failed")
        return detections


class _FakeDetector:
    def detect(self, image_uri: str) -> list[Detection]:
        _ = image_uri
        return []

    def warmup(self) -> None:
        return None

    def runtime_name(self) -> str:
        return "fake"


class _TypeErrorDetector:
    def detect(self, image_uri: str) -> list[Detection]:
        if isinstance(image_uri, str):
            return []
        raise TypeError("source must be string")

    def warmup(self) -> None:
        return None

    def runtime_name(self) -> str:
        return "type-error-fake"


class _FakeHttpResponse:
    def __init__(
        self, content_type: str = "", chunks: list[bytes] | None = None
    ) -> None:
        self.headers = {"content-type": content_type}
        self._chunks = chunks or []
        self.status_code = 200
        self.content = b"frame"
        self.closed = False

    def iter_bytes(self, chunk_size: int):
        _ = chunk_size
        return iter(self._chunks)

    def close(self) -> None:
        self.closed = True


class _FakeHttpClient:
    def __init__(self, timeout: float = 0.0) -> None:
        _ = timeout
        self._resp = _FakeHttpResponse()
        self.closed = False

    def build_request(self, method: str, url: str):
        return {"method": method, "url": url}

    def send(self, request, stream: bool = False):
        _ = (request, stream)
        return self._resp

    def get(self, url: str, timeout: float = 0.0):
        _ = (url, timeout)
        return self._resp

    def close(self) -> None:
        self.closed = True


def _settings() -> Settings:
    return Settings(
        app=AppSettings(),
        api=ApiSettings(),
        database=DatabaseSettings(),
        storage=StorageSettings(),
        rpi=RpiSettings(
            RPI_BASE_URL="http://127.0.0.1:9100",
            RPI_RTSP_PORT=8554,
            RPI_RTSP_PATH_PREFIX="live",
        ),
        detection=DetectionSettings(),
    )


def _state() -> online_main.RpiStreamState:
    return online_main.RpiStreamState(
        mission_id="m1",
        rpi_mission_id="rpi-1",
        session_id="s-1",
        rtsp_url="rtsp://127.0.0.1:8554/live/s-1",
        stream_url="http://127.0.0.1:9100/source/stream/s-1",
        target_fps=2.0,
        running=True,
        started_at="2026-01-01T00:00:00Z",
    )


def _pilot_service() -> PilotService:
    return cast(PilotService, _FakePilotService())


def test_gt_tracker_splits_episodes() -> None:
    tracker = online_main._GtTracker(sequence=[False, True, True, False, True])
    assert tracker.evaluate(0) == (False, None)
    assert tracker.evaluate(1) == (True, "ep-1")
    assert tracker.evaluate(2) == (True, "ep-1")
    assert tracker.evaluate(3) == (False, None)
    assert tracker.evaluate(4) == (True, "ep-2")


def test_build_loop_context_returns_none_when_capture_unavailable(monkeypatch) -> None:
    controller = online_main.DetectionStreamController(
        _settings(),
        pilot_service=_pilot_service(),
        detector=_FakeDetector(),
    )
    state = _state()
    monkeypatch.setattr(controller, "_load_gt_sequence", lambda _mid: [True, False])
    monkeypatch.setattr(controller, "_open_capture", lambda _state: None)

    ctx = controller._build_loop_context(
        mission_id="m1",
        state=state,
        stop_event=threading.Event(),
        target_fps=2.0,
    )

    assert ctx is None
    assert state.end_reason == "capture_open_failed"
    assert state.error is not None


def test_read_frame_with_recovery_stops_when_source_finished(monkeypatch) -> None:
    controller = online_main.DetectionStreamController(
        _settings(),
        pilot_service=_pilot_service(),
        detector=_FakeDetector(),
    )
    state = _state()
    ctx = online_main._LoopContext(
        mission_id="m1",
        state=state,
        stop_event=threading.Event(),
        target_fps=2.0,
        frame_interval=0.5,
        gt_tracker=online_main._GtTracker(sequence=[True]),
        source_filenames=None,
        capture=_FakeCapture([None]),
        tmp_dir=Path("."),
        consecutive_read_failures=8,
    )
    monkeypatch.setattr(controller, "_try_switch_to_http", lambda _ctx: False)
    monkeypatch.setattr(controller, "_stream_finished_on_rpi", lambda _state: True)

    frame, should_stop = controller._read_frame_with_recovery(ctx)

    assert frame is None
    assert should_stop is True
    assert state.end_reason == "source_finished"


def test_process_frame_updates_counters(monkeypatch, tmp_path) -> None:
    controller = online_main.DetectionStreamController(
        _settings(),
        pilot_service=_pilot_service(),
        detector=_FakeDetector(),
    )
    state = _state()
    ctx = online_main._LoopContext(
        mission_id="m1",
        state=state,
        stop_event=threading.Event(),
        target_fps=2.0,
        frame_interval=0.5,
        gt_tracker=online_main._GtTracker(sequence=[True]),
        source_filenames=None,
        capture=_FakeCapture([b"jpeg"]),
        tmp_dir=tmp_path,
    )
    monkeypatch.setattr(
        controller,
        "_detect_frame_or_empty",
        lambda **kwargs: [Detection((1.0, 2.0, 3.0, 4.0), 0.9, "person", "yolo", None)],
    )

    controller._process_frame(ctx, b"\xff\xd8\xff\xd9")

    assert ctx.frame_id == 1
    assert state.processed_frames == 1
    assert state.alerts_created == 1


def test_ingest_event_updates_error_on_failure() -> None:
    pilot = _FakePilotService()
    pilot.raise_on_ingest = True
    controller = online_main.DetectionStreamController(
        _settings(),
        pilot_service=cast(PilotService, pilot),
        detector=_FakeDetector(),
    )
    state = _state()
    ctx = online_main._LoopContext(
        mission_id="m1",
        state=state,
        stop_event=threading.Event(),
        target_fps=2.0,
        frame_interval=0.5,
        gt_tracker=online_main._GtTracker(sequence=[True]),
        source_filenames=None,
        capture=_FakeCapture([]),
        tmp_dir=Path("."),
    )
    frame_event = online_main.FrameEvent(
        mission_id="m1",
        frame_id=0,
        ts_sec=0.0,
        image_uri="file:///tmp/frame.jpg",
        gt_person_present=True,
        gt_episode_id="ep-1",
    )

    controller._ingest_event(ctx=ctx, frame_event=frame_event, detections=[])

    assert state.ingest_failures == 1
    assert state.error is not None


def test_read_frame_with_recovery_success_resets_failures() -> None:
    controller = online_main.DetectionStreamController(
        _settings(),
        pilot_service=_pilot_service(),
        detector=_FakeDetector(),
    )
    state = _state()
    ctx = online_main._LoopContext(
        mission_id="m1",
        state=state,
        stop_event=threading.Event(),
        target_fps=2.0,
        frame_interval=0.5,
        gt_tracker=online_main._GtTracker(sequence=[True]),
        source_filenames=None,
        capture=_FakeCapture([b"frame"]),
        tmp_dir=Path("."),
        consecutive_read_failures=3,
    )

    frame, should_stop = controller._read_frame_with_recovery(ctx)

    assert frame == b"frame"
    assert should_stop is False
    assert ctx.consecutive_read_failures == 0
    assert state.read_failures == 0


def test_read_frame_with_recovery_switches_to_http(monkeypatch) -> None:
    controller = online_main.DetectionStreamController(
        _settings(),
        pilot_service=_pilot_service(),
        detector=_FakeDetector(),
    )
    state = _state()
    ctx = online_main._LoopContext(
        mission_id="m1",
        state=state,
        stop_event=threading.Event(),
        target_fps=2.0,
        frame_interval=0.5,
        gt_tracker=online_main._GtTracker(sequence=[True]),
        source_filenames=None,
        capture=_FakeCapture([None]),
        tmp_dir=Path("."),
        consecutive_read_failures=8,
    )
    monkeypatch.setattr(controller, "_try_switch_to_http", lambda _ctx: True)

    frame, should_stop = controller._read_frame_with_recovery(ctx)

    assert frame is None
    assert should_stop is False


def test_detection_loop_handles_exception_and_finalizes(monkeypatch, tmp_path) -> None:
    controller = online_main.DetectionStreamController(
        _settings(),
        pilot_service=_pilot_service(),
        detector=_FakeDetector(),
    )
    state = _state()
    capture = _FakeCapture([])
    ctx = online_main._LoopContext(
        mission_id="m1",
        state=state,
        stop_event=threading.Event(),
        target_fps=2.0,
        frame_interval=0.5,
        gt_tracker=online_main._GtTracker(sequence=[True]),
        source_filenames=None,
        capture=capture,
        tmp_dir=tmp_path,
    )
    monkeypatch.setattr(controller, "_build_loop_context", lambda **_kwargs: ctx)

    def _boom(_ctx):
        raise RuntimeError("boom")

    monkeypatch.setattr(controller, "_read_frame_with_recovery", _boom)

    controller._detection_loop("m1", state, threading.Event())

    assert state.running is False
    assert state.end_reason == "loop_exception"
    assert capture.released is True


def test_detect_frame_falls_back_to_path_on_type_error(tmp_path) -> None:
    controller = online_main.DetectionStreamController(
        _settings(),
        pilot_service=_pilot_service(),
        detector=_TypeErrorDetector(),
    )
    detections = controller._detect_frame(
        frame=b"bytes-frame",
        fallback_path=tmp_path / "frame.jpg",
    )
    assert detections == []


def test_save_frame_raises_for_unsupported_type(tmp_path) -> None:
    with pytest.raises(TypeError, match="Unexpected frame type"):
        online_main.DetectionStreamController._save_frame(object(), tmp_path / "x.jpg")


def test_extract_source_filenames_from_annotations_payload() -> None:
    payload: dict[str, object] = {
        "images": [
            {"id": 2, "file_name": "frames/013203.jpg"},
            {"id": 1, "file_name": "frames/013202.jpg"},
        ]
    }
    filenames = online_main.DetectionStreamController._extract_source_filenames(payload)
    assert filenames == ["013202.jpg", "013203.jpg"]


def test_process_frame_uses_source_filename_from_annotations(
    monkeypatch, tmp_path
) -> None:
    controller = online_main.DetectionStreamController(
        _settings(),
        pilot_service=_pilot_service(),
        detector=_FakeDetector(),
    )
    state = _state()
    ctx = online_main._LoopContext(
        mission_id="m1",
        state=state,
        stop_event=threading.Event(),
        target_fps=2.0,
        frame_interval=0.5,
        gt_tracker=online_main._GtTracker(sequence=[True]),
        source_filenames=["013202.jpg"],
        capture=_FakeCapture([b"jpeg"]),
        tmp_dir=tmp_path,
    )
    monkeypatch.setattr(controller, "_detect_frame_or_empty", lambda **kwargs: [])
    ingested: dict[str, str] = {}

    def _capture_ingest(*, ctx, frame_event, detections):
        _ = (ctx, detections)
        ingested["image_uri"] = frame_event.image_uri

    monkeypatch.setattr(controller, "_ingest_event", _capture_ingest)

    controller._process_frame(ctx, b"\xff\xd8\xff\xd9")

    assert ingested["image_uri"].endswith("/013202.jpg")


def test_build_api_runtime_and_main(monkeypatch) -> None:
    settings = _settings()
    db = InMemoryDatabase()

    class _Inference:
        model_url = "https://example/model.pt"
        model_sha256 = "abc123"

    class _Contract:
        config_name = "test"
        config_hash = "hash"
        config_path = "configs/test.yaml"
        service_version = "dev"
        inference = _Inference()
        alert_rules = AlertRuleConfig(0.5, 1.0, 1, 0.0, 1.0, 1.0, 1.0)

    monkeypatch.setattr(online_main, "get_settings", lambda: settings)
    monkeypatch.setattr(
        online_main, "load_stream_contract", lambda **_kwargs: _Contract()
    )
    monkeypatch.setattr(
        online_main,
        "_build_repositories",
        lambda **_kwargs: (
            InMemoryMissionRepository(db),
            InMemoryAlertRepository(db),
            InMemoryFrameEventRepository(db),
            lambda: None,
        ),
    )
    monkeypatch.setattr(
        online_main,
        "build_s3_storage",
        lambda _storage: InMemoryArtifactStorage(),
    )

    def _build_detector():
        return _FakeDetector()

    monkeypatch.setattr(online_main, "_build_detector", _build_detector)

    pilot_service, stream_controller, reset_hook, detector = (
        online_main.build_api_runtime()
    )

    assert pilot_service is not None
    assert stream_controller is not None
    assert callable(reset_hook)
    assert detector is not None

    calls: dict[str, object] = {}
    monkeypatch.setattr(
        online_main,
        "_prepare_postgres_backend",
        lambda: calls.setdefault("prepared", True),
    )
    monkeypatch.setattr(
        online_main,
        "build_api_runtime",
        lambda: (pilot_service, stream_controller, reset_hook, detector),
    )
    monkeypatch.setattr(
        online_main,
        "set_runtime",
        lambda runtime: calls.setdefault("runtime", runtime),
    )
    monkeypatch.setattr(
        online_main.uvicorn,
        "run",
        lambda app, host, port: calls.setdefault("uvicorn", (app, host, port)),
    )

    online_main.main()

    assert calls["prepared"] is True
    assert calls["runtime"] is not None
    assert calls["uvicorn"] == (
        "rescue_ai.interfaces.api.app:app",
        settings.api.host,
        settings.api.port,
    )


def test_prepare_postgres_backend_validates_dsn(monkeypatch) -> None:
    settings = _settings()
    settings.database.dsn = ""
    monkeypatch.setattr(online_main, "get_settings", lambda: settings)
    with pytest.raises(RuntimeError, match="DB_DSN is required"):
        online_main._prepare_postgres_backend()

    settings.database.dsn = "postgresql://user:pass@localhost:5432/db"
    called: dict[str, object] = {}
    monkeypatch.setattr(
        online_main,
        "wait_for_postgres",
        lambda dsn, timeout_sec: called.setdefault("args", (dsn, timeout_sec)),
    )
    online_main._prepare_postgres_backend()
    assert called["args"] == (
        settings.database.dsn,
        settings.api.postgres_ready_timeout_sec,
    )


def test_http_frame_capture_handles_mjpeg_and_single_frame(monkeypatch) -> None:
    class _MjpegClient(_FakeHttpClient):
        def __init__(self, timeout: float = 0.0) -> None:
            super().__init__(timeout=timeout)
            self._resp = _FakeHttpResponse(
                content_type="multipart/x-mixed-replace",
                chunks=[b"noise", b"\xff\xd8abc\xff\xd9tail"],
            )

    monkeypatch.setattr(online_main.httpx, "Client", _MjpegClient)
    capture = online_main._HttpFrameCapture("http://cam/stream")
    assert capture.is_open() is True
    assert capture.read_frame() == b"\xff\xd8abc\xff\xd9"
    capture.release()

    class _SingleClient(_FakeHttpClient):
        def __init__(self, timeout: float = 0.0) -> None:
            super().__init__(timeout=timeout)
            self._resp = _FakeHttpResponse(content_type="text/plain")

    monkeypatch.setattr(online_main.httpx, "Client", _SingleClient)
    capture = online_main._HttpFrameCapture("http://cam/frame")
    assert capture.is_open() is True
    assert capture.read_frame() == b"frame"
    capture.release()


def test_http_frame_capture_handles_connect_error(monkeypatch) -> None:
    class _BrokenClient(_FakeHttpClient):
        def send(self, request, stream: bool = False):
            _ = (request, stream)
            raise online_main.httpx.HTTPError("connection failed")

    monkeypatch.setattr(online_main.httpx, "Client", _BrokenClient)
    capture = online_main._HttpFrameCapture("http://cam/stream")
    assert capture.is_open() is False


def test_rtsp_capture_uses_cv2_and_reads_frame(monkeypatch) -> None:
    class _Cap:
        def __init__(self, opened: bool) -> None:
            self._opened = opened
            self.released = False
            setattr(self, "isOpened", self.is_opened)

        def is_opened(self) -> bool:
            return self._opened

        def read(self):
            return True, "frame"

        def release(self) -> None:
            self.released = True

    class _Cv2:
        CAP_FFMPEG = 1900

        def __init__(self) -> None:
            self.calls = 0
            setattr(self, "VideoCapture", self.video_capture)

        def video_capture(self, _url, _backend):
            self.calls += 1
            return _Cap(opened=self.calls > 1)

    monkeypatch.setitem(sys.modules, "cv2", _Cv2())
    cap = online_main._RtspFrameCapture("rtsp://example/stream")
    assert cap.is_open() is True
    assert cap.read_frame() == "frame"
    cap.release()


def test_detect_frame_without_detector_and_save_frame_numpy(
    monkeypatch, tmp_path
) -> None:
    controller = online_main.DetectionStreamController(
        _settings(),
        pilot_service=None,
        detector=None,
    )
    with pytest.raises(RuntimeError, match="Detector is not configured"):
        controller._detect_frame(frame="x", fallback_path=tmp_path / "x.jpg")

    class _Cv2:
        @staticmethod
        def imwrite(path: str, frame) -> bool:
            Path(path).write_bytes(b"ok")
            _ = frame
            return True

    monkeypatch.setattr(online_main, "import_module", lambda _name: _Cv2)
    online_main.DetectionStreamController._save_frame(
        np.zeros((2, 2, 3), dtype=np.uint8),
        tmp_path / "frame.jpg",
    )
    assert (tmp_path / "frame.jpg").exists()


def test_build_detector_and_build_repositories_error_paths(monkeypatch) -> None:
    monkeypatch.setattr(
        online_main,
        "load_stream_contract",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("bad contract")),
    )
    assert online_main._build_detector() is None

    settings = _settings()
    settings.database.dsn = "   "
    with pytest.raises(ValueError, match="DB_DSN is required"):
        online_main._build_repositories(settings=settings)
