"""Online API server entry point."""

from __future__ import annotations

import logging
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from rescue_ai.application.pilot_service import PilotService
from rescue_ai.config import Settings, get_settings
from rescue_ai.domain.entities import Detection, FrameEvent
from rescue_ai.domain.ports import AlertRepository
from rescue_ai.domain.ports import DetectorPort as DomainDetectorPort
from rescue_ai.domain.ports import (
    FrameEventRepository,
    MissionRepository,
    ReportMetadataPayload,
)
from rescue_ai.infrastructure.artifact_storage import build_s3_storage
from rescue_ai.infrastructure.contract_loader import load_stream_contract
from rescue_ai.infrastructure.postgres_connection import wait_for_postgres
from rescue_ai.infrastructure.rpi_client import RpiClient
from rescue_ai.interfaces.api.dependencies import ApiRuntime, set_runtime

logger = logging.getLogger(__name__)


@dataclass
class RpiStreamState:
    """RPi streaming session state bound to one mission."""

    mission_id: str
    rpi_mission_id: str
    session_id: str
    rtsp_url: str
    stream_url: str
    target_fps: float
    running: bool
    started_at: str
    processed_frames: int = 0
    alerts_created: int = 0
    ingest_failures: int = 0
    detection_failures: int = 0
    capture_backend: str | None = None
    gt_sequence_total: int | None = None
    source_frames_total: int | None = None
    read_failures: int = 0
    end_reason: str | None = None
    last_stats: dict[str, object] | None = None
    error: str | None = None


@dataclass
class _GtTracker:
    sequence: list[bool] | None
    episode_id: int = 0
    prev_present: bool = False

    def evaluate(self, frame_id: int) -> tuple[bool, str | None]:
        gt_present = bool(
            self.sequence is not None
            and frame_id < len(self.sequence)
            and self.sequence[frame_id]
        )
        if gt_present and not self.prev_present:
            self.episode_id += 1
        self.prev_present = gt_present
        return gt_present, (f"ep-{self.episode_id}" if gt_present else None)


@dataclass
class _LoopContext:
    mission_id: str
    state: RpiStreamState
    stop_event: threading.Event
    target_fps: float
    frame_interval: float
    gt_tracker: _GtTracker
    capture: _FrameCapture
    tmp_dir: Path
    frame_id: int = 0
    consecutive_read_failures: int = 0


class _FrameCapture:
    """Base interface for frame capture backends."""

    def read_frame(self) -> object | None:
        raise NotImplementedError

    def is_open(self) -> bool:
        raise NotImplementedError

    def release(self) -> None:
        pass


class _HttpFrameCapture(_FrameCapture):
    """Capture frames via RPi HTTP streaming endpoint (MJPEG or JPEG-per-request)."""

    def __init__(self, stream_url: str) -> None:
        self._url = stream_url
        self._client: httpx.Client | None = None
        self._response: httpx.Response | None = None
        self._stream_iter: Iterator[bytes] | None = None
        self._buffer = b""
        self._ok = False
        try:
            # Try to connect with streaming to detect MJPEG
            self._client = httpx.Client(timeout=10.0)
            resp = self._client.send(
                self._client.build_request("GET", self._url),
                stream=True,
            )
            content_type = resp.headers.get("content-type", "")
            if "multipart" in content_type or "image" in content_type:
                self._response = resp
                self._content_type = content_type
                self._ok = True
                self._stream_iter = resp.iter_bytes(chunk_size=16384)
            else:
                # Not a stream — try frame-by-frame polling
                resp.close()
                self._response = None
                self._content_type = ""
                self._ok = True
                self._stream_iter = None
        except (httpx.HTTPError, RuntimeError, ValueError) as err:
            logger.warning("HTTP stream connect failed: %s", err)
            self._ok = False

    def is_open(self) -> bool:
        return self._ok

    def read_frame(self) -> bytes | None:
        """Read one JPEG frame from the HTTP stream."""
        try:
            if self._stream_iter is not None:
                return self._read_mjpeg_frame()
            return self._read_single_frame()
        except (httpx.HTTPError, RuntimeError, ValueError) as err:
            logger.warning("HTTP frame read error: %s", err)
            return None

    def _read_mjpeg_frame(self) -> bytes | None:
        """Parse JPEG frames from multipart MJPEG stream."""
        stream_iter = self._stream_iter
        if stream_iter is None:
            return None
        while True:
            try:
                chunk = next(stream_iter)
            except StopIteration:
                return None
            self._buffer += chunk

            # Find JPEG boundaries
            start = self._buffer.find(b"\xff\xd8")
            if start == -1:
                # No JPEG start yet, keep only last 2 bytes
                self._buffer = self._buffer[-2:]
                continue
            end = self._buffer.find(b"\xff\xd9", start + 2)
            if end == -1:
                continue

            # Extract complete JPEG
            end_idx = end + 2
            jpeg = self._buffer[start:end_idx]
            self._buffer = self._buffer[end_idx:]
            return jpeg

    def _read_single_frame(self) -> bytes | None:
        """Poll a single JPEG frame from the HTTP endpoint."""
        if self._client is None:
            return None
        resp = self._client.get(self._url, timeout=2.0)
        if resp.status_code == 200 and resp.content:
            return resp.content
        return None

    def release(self) -> None:
        if self._response is not None:
            with suppress(Exception):
                self._response.close()
        if self._client is not None:
            with suppress(Exception):
                self._client.close()


class _RtspFrameCapture(_FrameCapture):
    """Capture frames via RTSP using OpenCV."""

    def __init__(self, rtsp_url: str) -> None:
        import os

        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        try:
            import cv2

            self._cv2 = cv2
            self._cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
            if not self._cap.isOpened():
                time.sleep(2.0)
                self._cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
        except ImportError:
            self._cap = None
            self._cv2 = None

    def is_open(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    def read_frame(self) -> object | None:
        if self._cap is None:
            return None
        ret, frame = self._cap.read()
        return frame if ret else None

    def release(self) -> None:
        if self._cap is not None:
            self._cap.release()


class DetectionStreamController:
    """Controls RPi streaming + server-side YOLO detection pipeline.

    On ``start()`` the controller:
    1. Tells RPi to begin an RTSP stream for the chosen mission.
    2. Spawns a background thread that captures frames from the RTSP URL
       via OpenCV, runs YoloDetector on each frame, and calls
       ``PilotService.ingest_frame_event`` which creates alerts.

    On ``stop()`` it signals the background thread to exit and tells RPi
    to stop the stream.
    """

    def __init__(
        self,
        settings: Settings,
        pilot_service: PilotService | None = None,
        detector: DomainDetectorPort | None = None,
    ) -> None:
        self._rpi_settings = settings.rpi
        self._sessions: dict[str, RpiStreamState] = {}
        self._stop_events: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._pilot_service = pilot_service
        self._detector = detector

    def start(
        self,
        *,
        mission_id: str,
        rpi_mission_id: str,
        target_fps: float,
    ) -> RpiStreamState:
        current = self._sessions.get(mission_id)
        if current is not None and current.running:
            raise ValueError("Stream already running for mission")

        session = self._client().start_stream(
            mission_id=rpi_mission_id,
            target_fps=target_fps,
            timeout_sec=self._rpi_settings.timeout_sec,
        )
        state = RpiStreamState(
            mission_id=mission_id,
            rpi_mission_id=rpi_mission_id,
            session_id=session.session_id,
            rtsp_url=session.rtsp_url,
            stream_url=getattr(session, "stream_url", ""),
            target_fps=target_fps,
            running=True,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        self._sessions[mission_id] = state

        if self._pilot_service is None or self._detector is None:
            state.error = "detector or pilot_service not configured"
            logger.warning(
                "Stream started without detection pipeline: mission=%s",
                mission_id,
            )
            return state

        # Launch background detection pipeline
        stop_event = threading.Event()
        self._stop_events[mission_id] = stop_event
        thread = threading.Thread(
            target=self._detection_loop,
            args=(mission_id, state, stop_event),
            daemon=True,
            name=f"detect-{mission_id[:8]}",
        )
        self._threads[mission_id] = thread
        thread.start()

        return state

    def stop(self, mission_id: str) -> RpiStreamState | None:
        state = self._sessions.get(mission_id)
        if state is None:
            return None

        # Signal background thread to stop
        stop_event = self._stop_events.get(mission_id)
        if stop_event is not None:
            if state.end_reason is None:
                state.end_reason = "stop_requested"
            stop_event.set()

        # Wait for thread to finish (with timeout)
        thread = self._threads.get(mission_id)
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

        if state.running:
            try:
                self._client().stop_stream(
                    state.session_id, timeout_sec=self._rpi_settings.timeout_sec
                )
            except (ValueError, RuntimeError, OSError) as error:
                state.error = f"{type(error).__name__}: {error}"

        state.running = False
        if state.end_reason is None:
            state.end_reason = "stop_requested"
        return state

    def get_state(self, mission_id: str) -> RpiStreamState | None:
        state = self._sessions.get(mission_id)
        if state is None:
            return None
        if not state.running:
            return state

        try:
            stats = self._client().session_stats(
                state.session_id, timeout_sec=self._rpi_settings.timeout_sec
            )
            state.last_stats = stats
            total_source_frames = stats.get("total_source_frames")
            if isinstance(total_source_frames, int) and total_source_frames > 0:
                state.source_frames_total = total_source_frames
        except (ValueError, RuntimeError, OSError) as error:
            state.error = f"{type(error).__name__}: {error}"
        return state

    def as_payload(self, mission_id: str) -> dict[str, object] | None:
        state = self.get_state(mission_id)
        if state is None:
            return None
        return asdict(state)

    def check_rpi_health(self) -> dict[str, object]:
        return self._client().health(timeout_sec=self._rpi_settings.timeout_sec)

    def list_rpi_missions(self) -> list[dict[str, str]]:
        catalog = self._client().catalog(timeout_sec=self._rpi_settings.timeout_sec)
        return [
            {"mission_id": mission.mission_id, "name": mission.name}
            for mission in catalog.missions
        ]

    def _client(self) -> RpiClient:
        return RpiClient(self._rpi_settings)

    # ── Background RTSP → YOLO → ingest pipeline ──────────────────

    def _detection_loop(
        self,
        mission_id: str,
        state: RpiStreamState,
        stop_event: threading.Event,
    ) -> None:
        """Background thread: capture frames from RPi, detect, ingest."""
        if self._pilot_service is None or self._detector is None:
            state.error = "detector or pilot_service not configured"
            state.end_reason = "detector_or_service_missing"
            state.running = False
            logger.error("Detection loop cannot run without detector and pilot_service")
            return

        target_fps = state.target_fps
        ctx = self._build_loop_context(
            mission_id=mission_id,
            state=state,
            stop_event=stop_event,
            target_fps=target_fps,
        )
        if ctx is None:
            return

        try:
            while not stop_event.is_set():
                total = state.source_frames_total
                if total is not None and ctx.frame_id >= total:
                    state.end_reason = "source_finished"
                    break

                t0 = time.monotonic()
                frame, should_stop = self._read_frame_with_recovery(ctx)
                if should_stop:
                    break
                if frame is None:
                    continue

                self._process_frame(ctx, frame)

                # Clean up previous frame file to save disk
                if ctx.frame_id > 1:
                    prev = ctx.tmp_dir / f"frame_{ctx.frame_id - 2:06d}.jpg"
                    prev.unlink(missing_ok=True)

                # Throttle to target FPS
                elapsed = time.monotonic() - t0
                sleep_time = ctx.frame_interval - elapsed
                if sleep_time > 0:
                    stop_event.wait(timeout=sleep_time)

        except (RuntimeError, ValueError, TypeError, OSError) as loop_err:
            state.error = f"{type(loop_err).__name__}: {loop_err}"
            state.end_reason = "loop_exception"
            logger.exception("Detection loop crashed: %s", loop_err)
        finally:
            with suppress(Exception):
                ctx.capture.release()
            state.running = False
            if state.end_reason is None and state.error is None:
                state.end_reason = "source_finished"
            self._finalize_mission_after_stream_end(ctx)
            for f in ctx.tmp_dir.glob("*.jpg"):
                f.unlink(missing_ok=True)
            ctx.tmp_dir.rmdir()
            logger.info(
                "Detection loop finished: mission=%s frames=%d alerts=%d",
                mission_id,
                ctx.frame_id,
                state.alerts_created,
            )

    def _finalize_mission_after_stream_end(self, ctx: _LoopContext) -> None:
        """Try to auto-complete mission when source naturally finished."""
        if self._pilot_service is None:
            return
        if ctx.state.end_reason != "source_finished":
            return
        completed_frame_id: int | None = (
            ctx.state.processed_frames - 1 if ctx.state.processed_frames > 0 else None
        )
        try:
            self._pilot_service.complete_mission(
                mission_id=ctx.mission_id,
                completed_frame_id=completed_frame_id,
            )
            logger.info(
                "Mission auto-completed after source finish: mission=%s frame_id=%s",
                ctx.mission_id,
                completed_frame_id,
            )
        except AttributeError:
            return
        except ValueError as error:
            # Usually means queued alerts exist; mission stays open for review.
            ctx.state.end_reason = "source_finished_pending_alert_review"
            logger.warning(
                "Mission not auto-completed after source finish: mission=%s reason=%s",
                ctx.mission_id,
                error,
            )

    def _build_loop_context(
        self,
        *,
        mission_id: str,
        state: RpiStreamState,
        stop_event: threading.Event,
        target_fps: float,
    ) -> _LoopContext | None:
        frame_interval = 1.0 / target_fps if target_fps > 0 else 0.5
        gt_sequence = self._load_gt_sequence(state.rpi_mission_id)
        state.gt_sequence_total = len(gt_sequence) if gt_sequence is not None else None
        capture = self._open_capture(state)
        if capture is None:
            state.error = "Cannot open stream (tried HTTP and RTSP)"
            state.end_reason = "capture_open_failed"
            state.running = False
            return None
        state.capture_backend = (
            "rtsp" if isinstance(capture, _RtspFrameCapture) else "http"
        )
        return _LoopContext(
            mission_id=mission_id,
            state=state,
            stop_event=stop_event,
            target_fps=target_fps,
            frame_interval=frame_interval,
            gt_tracker=_GtTracker(sequence=gt_sequence),
            capture=capture,
            tmp_dir=Path(tempfile.mkdtemp(prefix="rescue_frames_")),
        )

    def _read_frame_with_recovery(
        self,
        ctx: _LoopContext,
    ) -> tuple[object | None, bool]:
        frame = ctx.capture.read_frame()
        if frame is not None:
            ctx.state.read_failures = 0
            ctx.consecutive_read_failures = 0
            return frame, False

        ctx.consecutive_read_failures += 1
        ctx.state.read_failures = ctx.consecutive_read_failures
        if ctx.stop_event.is_set():
            ctx.state.end_reason = "stop_requested"
            return None, True

        if ctx.consecutive_read_failures >= 8:
            switched = self._try_switch_to_http(ctx)
            if switched:
                return None, False
            if self._stream_finished_on_rpi(ctx.state):
                ctx.state.end_reason = "source_finished"
                logger.info(
                    "RPi stream finished mission=%s after %d read failures",
                    ctx.mission_id,
                    ctx.consecutive_read_failures,
                )
                return None, True

        retry_delay = 0.15 if isinstance(ctx.capture, _HttpFrameCapture) else 1.0
        logger.warning("Frame read failed, retrying in %.2fs...", retry_delay)
        time.sleep(retry_delay)
        return None, False

    def _try_switch_to_http(self, ctx: _LoopContext) -> bool:
        if not isinstance(ctx.capture, _RtspFrameCapture):
            return False
        switched = self._switch_capture_to_http(
            current_capture=ctx.capture,
            state=ctx.state,
        )
        if switched is None:
            return False
        ctx.capture = switched
        ctx.consecutive_read_failures = 0
        ctx.state.read_failures = 0
        return True

    def _process_frame(self, ctx: _LoopContext, frame: object) -> None:
        frame_path = ctx.tmp_dir / f"frame_{ctx.frame_id:06d}.jpg"
        self._save_frame(frame, frame_path)
        ts_sec = (
            ctx.frame_id / ctx.target_fps if ctx.target_fps > 0 else ctx.frame_id * 0.5
        )
        gt_present, gt_episode_id = ctx.gt_tracker.evaluate(ctx.frame_id)
        detections = self._detect_frame_or_empty(
            frame=frame,
            frame_path=frame_path,
            frame_id=ctx.frame_id,
            state=ctx.state,
        )
        frame_event = FrameEvent(
            mission_id=ctx.mission_id,
            frame_id=ctx.frame_id,
            ts_sec=ts_sec,
            image_uri=str(frame_path),
            gt_person_present=gt_present,
            gt_episode_id=gt_episode_id,
        )
        self._ingest_event(ctx=ctx, frame_event=frame_event, detections=detections)
        ctx.frame_id += 1
        ctx.state.processed_frames = ctx.frame_id

    def _detect_frame_or_empty(
        self,
        *,
        frame: object,
        frame_path: Path,
        frame_id: int,
        state: RpiStreamState,
    ) -> list[Detection]:
        try:
            return self._detect_frame(frame=frame, fallback_path=frame_path)
        except (RuntimeError, ValueError, TypeError, OSError) as det_err:
            logger.warning("Detection error frame=%d: %s", frame_id, det_err)
            state.detection_failures += 1
            return []

    def _ingest_event(
        self,
        *,
        ctx: _LoopContext,
        frame_event: FrameEvent,
        detections: list[Detection],
    ) -> None:
        if self._pilot_service is None:
            raise RuntimeError("PilotService is not configured")
        try:
            alerts = self._pilot_service.ingest_frame_event(
                frame_event=frame_event,
                detections=detections,
            )
            ctx.state.alerts_created += len(alerts)
        except (RuntimeError, ValueError, TypeError, OSError) as ingest_err:
            logger.warning("Ingest error frame=%d: %s", ctx.frame_id, ingest_err)
            ctx.state.ingest_failures += 1
            ctx.state.error = f"{type(ingest_err).__name__}: {ingest_err}"

    def _stream_finished_on_rpi(self, state: RpiStreamState) -> bool:
        try:
            stats = self._client().session_stats(
                state.session_id,
                timeout_sec=self._rpi_settings.timeout_sec,
            )
        except (ValueError, RuntimeError, OSError):
            return False
        state.last_stats = stats
        total_source_frames = stats.get("total_source_frames")
        if isinstance(total_source_frames, int) and total_source_frames > 0:
            state.source_frames_total = total_source_frames
        publisher_error = str(stats.get("publisher_error", "")).strip()
        if publisher_error:
            state.error = publisher_error
        return bool(stats.get("stop", False))

    def _switch_capture_to_http(
        self,
        *,
        current_capture: _FrameCapture,
        state: RpiStreamState,
    ) -> _FrameCapture | None:
        if not state.stream_url:
            return None
        logger.warning(
            "Switching capture backend mission=%s rtsp->http after read failures",
            state.mission_id,
        )
        http_capture = _HttpFrameCapture(state.stream_url)
        if not http_capture.is_open():
            return None
        with suppress(Exception):
            current_capture.release()
        state.capture_backend = "http"
        return http_capture

    def _load_gt_sequence(self, rpi_mission_id: str) -> list[bool] | None:
        try:
            return self._client().load_gt_sequence(
                rpi_mission_id,
                timeout_sec=self._rpi_settings.timeout_sec,
            )
        except (ValueError, RuntimeError, OSError) as error:
            logger.warning(
                "Cannot load GT sequence mission=%s: %s: %s",
                rpi_mission_id,
                type(error).__name__,
                error,
            )
            return None

    def _detect_frame(
        self,
        *,
        frame: object,
        fallback_path: Path,
    ) -> list[Detection]:
        detector = self._detector
        if detector is None:
            raise RuntimeError("Detector is not configured")
        detector_any: Any = detector
        try:
            return detector_any.detect(frame)
        except TypeError:
            return detector_any.detect(str(fallback_path))

    @staticmethod
    def _save_frame(frame: object, path: Path) -> None:
        """Save a numpy frame (from cv2) or raw bytes (from HTTP) to JPEG."""
        if isinstance(frame, bytes):
            path.write_bytes(frame)
            return

        import numpy as np

        if isinstance(frame, np.ndarray):
            cv2 = import_module("cv2")

            cv2.imwrite(str(path), frame)
            return
        raise TypeError(f"Unexpected frame type: {type(frame)}")

    def _open_capture(self, state: RpiStreamState) -> _FrameCapture | None:
        """Try RTSP first (low-latency), then HTTP as fallback."""
        # 1. RTSP primary path
        if state.rtsp_url:
            logger.info("Trying RTSP: %s", state.rtsp_url)
            rtsp_capture: _FrameCapture = _RtspFrameCapture(state.rtsp_url)
            if rtsp_capture.is_open():
                logger.info("RTSP stream opened successfully")
                return rtsp_capture
            logger.warning("RTSP stream failed, trying HTTP fallback...")

        # 2. HTTP fallback (MJPEG or polling endpoint)
        if state.stream_url:
            logger.info("Trying HTTP stream: %s", state.stream_url)
            http_capture: _FrameCapture = _HttpFrameCapture(state.stream_url)
            if http_capture.is_open():
                logger.info("HTTP stream opened successfully")
                return http_capture
            logger.warning("HTTP stream also failed")

        return None


def _build_detector() -> DomainDetectorPort | None:
    """Create YoloDetector from stream contract config (lazy, optional)."""
    try:
        from rescue_ai.infrastructure.yolo_detector import YoloDetector

        settings = get_settings()
        contract = load_stream_contract(
            service_version=settings.app.service_version,
        )
        detector = YoloDetector(config=contract.inference)
        logger.info(
            "YoloDetector initialized (model_url=%s)", contract.inference.model_url
        )
        return detector
    except (ImportError, RuntimeError, ValueError, TypeError, OSError) as error:
        logger.warning(
            "YoloDetector not available: %s: %s", type(error).__name__, error
        )
        return None


def build_api_runtime() -> tuple[
    PilotService,
    DetectionStreamController,
    Callable[[], None],
    DomainDetectorPort | None,
]:
    """Assemble API runtime dependencies (composition root)."""
    settings = get_settings()
    contract = load_stream_contract(
        service_version=settings.app.service_version,
    )
    alert_rules = contract.alert_rules

    report_metadata: ReportMetadataPayload = {
        "config_name": contract.config_name,
        "config_hash": contract.config_hash,
        "config_path": contract.config_path,
        "model_url": contract.inference.model_url,
        "service_version": contract.service_version,
    }
    if contract.inference.model_sha256:
        report_metadata["model_sha256"] = contract.inference.model_sha256

    mission_repository, alert_repository, frame_repository, reset_hook = (
        _build_repositories(settings=settings)
    )

    artifact_storage = build_s3_storage(settings.storage)

    pilot_service = PilotService(
        dependencies=PilotService.Dependencies(
            mission_repository=mission_repository,
            alert_repository=alert_repository,
            frame_event_repository=frame_repository,
            artifact_storage=artifact_storage,
        ),
        alert_rules=alert_rules,
    )
    pilot_service.set_report_metadata(report_metadata)

    detector = _build_detector()

    stream_controller = DetectionStreamController(
        settings=settings,
        pilot_service=pilot_service,
        detector=detector,
    )
    return pilot_service, stream_controller, reset_hook, detector


def main() -> None:
    """Start the API server and initialize runtime dependencies."""
    settings = get_settings()
    _prepare_postgres_backend()
    pilot_service, stream_controller, reset_hook, detector = build_api_runtime()
    set_runtime(
        ApiRuntime(
            pilot_service=pilot_service,
            stream_controller=stream_controller,
            reset_hook=reset_hook,
            detector=detector,
        )
    )
    uvicorn.run(
        "rescue_ai.interfaces.api.app:app",
        host=settings.api.host,
        port=settings.api.port,
    )


def _prepare_postgres_backend() -> None:
    settings = get_settings()
    dsn = settings.database.dsn
    if not dsn:
        raise RuntimeError("DB_DSN is required")
    wait_for_postgres(dsn, timeout_sec=settings.api.postgres_ready_timeout_sec)


def _build_repositories(
    *,
    settings: Settings,
) -> tuple[
    MissionRepository,
    AlertRepository,
    FrameEventRepository,
    Callable[[], None],
]:
    from rescue_ai.infrastructure.postgres_connection import PostgresDatabase
    from rescue_ai.infrastructure.postgres_repositories import (
        PostgresAlertRepository,
        PostgresFrameEventRepository,
        PostgresMissionRepository,
    )

    dsn = settings.database.dsn.strip()
    if not dsn:
        raise ValueError("DB_DSN is required")

    postgres_db = PostgresDatabase(dsn=dsn, schema="app")
    return (
        PostgresMissionRepository(postgres_db),
        PostgresAlertRepository(postgres_db, episode_settings=None),
        PostgresFrameEventRepository(postgres_db, episode_settings=None),
        postgres_db.truncate_all,
    )


if __name__ == "__main__":
    main()
