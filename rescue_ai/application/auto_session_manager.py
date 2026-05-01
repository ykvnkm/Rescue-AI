"""Session runtime for automatic missions streamed from the UI.

Wraps :class:`AutoMissionService` push-ingest with a per-session
background thread that pulls frames from a :class:`VideoFramePort` and
fans out compact JSON snapshots to subscribed observers (WebSocket
clients). Only one session may be active at a time — the underlying
:class:`NavigationEnginePort` is not safe to share across concurrent
sessions.

The thread owns:

* a ``stop_event`` the caller sets to request an early stop;
* a list of ``subscribers`` (callables receiving dict payloads);
* simple stats (frames consumed / emitted, avg fps, last error).

Each event is a dict with ``type`` ∈ ``{"ready","frame","done","error"}``.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from rescue_ai.application.auto_mission_service import (
    AutoFrameOutcome,
    AutoMissionService,
)
from rescue_ai.domain.entities import Mission
from rescue_ai.domain.value_objects import NavMode

logger = logging.getLogger(__name__)

Subscriber = Callable[[Mapping[str, Any]], None]


class VideoFramePort(Protocol):
    """Runtime video source protocol consumed by auto sessions."""

    def frames(self) -> Iterator[tuple[object, float, int]]: ...

    def close(self) -> None: ...


# (source_kind, source_value, fps, rpi_mission_id, demo_loop)
# -> (VideoFramePort, resolved_value)
# ``rpi_mission_id`` is non-empty only when the caller wants a remote RPi stream;
# in that case the factory must wrap ``RpiClient.start_stream`` and ignore
# ``source_value`` (it stays "" in the stream channel). ``demo_loop`` applies
# only to local file sources — it asks ``FileVideoSource`` to auto-restart on EOF.
VideoSourceFactory = Callable[
    [str, str, float | None, str, bool],
    tuple[VideoFramePort, str, float],
]


@dataclass
class AutoSessionStats:
    """Running counters for one automatic session."""

    frames_consumed: int = 0
    frames_emitted: int = 0
    frames_dropped: int = 0
    alerts_total: int = 0
    started_at_monotonic: float = 0.0
    last_frame_at_monotonic: float = 0.0
    avg_stream_fps: float = 0.0
    last_error: str | None = None


@dataclass
class AutoSessionInfo:
    """Immutable-ish descriptor returned to API callers."""

    session_id: str
    mission_id: str
    source_kind: str
    source_value: str
    nav_mode: str
    detector_name: str
    fps: float
    started_at: str


@dataclass(frozen=True)
class AutoSessionInit:
    """Static parameters required to construct one :class:`AutoSession`."""

    session_id: str
    mission: Mission
    source: VideoFramePort
    source_kind: str
    source_value: str
    nav_mode: NavMode
    detector_name: str
    detect_enabled: bool = True
    save_video: bool = False
    save_video_dir: str | None = None


@dataclass(frozen=True)
class StartSessionRequest:
    """Input payload for :meth:`AutoSessionManager.start_session`.

    Automatic-mode behavior flags (``nsu_channel``, ``rpi_mission_id``,
    ``demo_loop``) travel inside ``config_json`` so they're persisted on
    the Mission record. Session-runtime flags (``detect_enabled``,
    ``save_video``) are passed explicitly to :class:`AutoSession` because
    they influence per-frame processing in the session thread.
    """

    source: VideoFramePort
    source_kind: str
    source_value: str
    nav_mode: NavMode
    detector_name: str
    fps: float
    total_frames: int = 0
    config_json: Mapping[str, object] | None = None
    detect_enabled: bool = True
    save_video: bool = False


@dataclass
class _SubscribersBundle:
    """Thread-safe subscriber list with a lock."""

    items: list[Subscriber] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, subscriber: Subscriber) -> None:
        with self.lock:
            self.items.append(subscriber)

    def remove(self, subscriber: Subscriber) -> None:
        with self.lock:
            try:
                self.items.remove(subscriber)
            except ValueError:
                pass

    def snapshot(self) -> list[Subscriber]:
        with self.lock:
            return list(self.items)


class _JpegEncoder:
    """Encode BGR frames to base64-JPEG with width cap (lazy-imports cv2)."""

    def __init__(self, *, max_width: int, quality: int) -> None:
        self._max_width = max(0, int(max_width))
        self._quality = max(1, min(100, int(quality)))

    def encode(self, frame_bgr: object) -> str | None:
        try:
            import cv2  # noqa: WPS433
        except ImportError:  # pragma: no cover - optional dep at runtime
            return None

        frame = cast(Any, frame_bgr)
        try:
            height, width = frame.shape[:2]
        except (AttributeError, TypeError, ValueError):  # pragma: no cover
            return None
        if self._max_width > 0 and width > self._max_width:
            scale = self._max_width / float(width)
            new_size = (self._max_width, max(1, int(round(height * scale))))
            frame = cv2.resize(frame, new_size, interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self._quality]
        )
        if not ok:
            return None
        return base64.b64encode(buf.tobytes()).decode("ascii")


class AutoSession:
    """One active automatic-mode session running on a background thread."""

    def __init__(
        self,
        *,
        init: AutoSessionInit,
        service: AutoMissionService,
        encoder: _JpegEncoder,
        emit_max_fps: float,
        on_finished: Callable[["AutoSession"], None],
    ) -> None:
        self.session_id = init.session_id
        self.mission = init.mission
        self._source = init.source
        self.source_kind = init.source_kind
        self.source_value = init.source_value
        self.nav_mode = init.nav_mode
        self.detector_name = init.detector_name
        self._detect_enabled = bool(init.detect_enabled)
        self._save_video = bool(init.save_video)
        self._save_video_dir = init.save_video_dir
        self._service = service
        self._encoder = encoder
        self._emit_min_dt = 1.0 / emit_max_fps if emit_max_fps > 0 else 0.0
        self._on_finished = on_finished

        self._stop_event = threading.Event()
        self._done_event = threading.Event()
        self._subscribers = _SubscribersBundle()
        self._stats = AutoSessionStats()
        self._video_writer: Any | None = None
        self._video_writer_path: str | None = None
        self._stream_stats_cache: dict[str, object] = {}
        self._last_stream_stats_poll: float = 0.0
        self._stream_stats_fn: Callable[[], Mapping[str, object]] | None = None
        stats_fn = getattr(init.source, "session_stats", None)
        if callable(stats_fn):
            self._stream_stats_fn = cast(Callable[[], Mapping[str, object]], stats_fn)
        self._thread = threading.Thread(
            target=self._run, name=f"auto-session-{init.session_id}", daemon=True
        )

    # ── Public API ────────────────────────────────────────────────

    @property
    def mission_id(self) -> str:
        return self.mission.mission_id

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def info(self) -> AutoSessionInfo:
        return AutoSessionInfo(
            session_id=self.session_id,
            mission_id=self.mission.mission_id,
            source_kind=self.source_kind,
            source_value=self.source_value,
            nav_mode=str(self.nav_mode),
            detector_name=self.detector_name,
            fps=self.mission.fps,
            started_at=self.mission.created_at,
        )

    def stats(self) -> AutoSessionStats:
        # Return a copy to avoid external mutation.
        return AutoSessionStats(
            frames_consumed=self._stats.frames_consumed,
            frames_emitted=self._stats.frames_emitted,
            frames_dropped=self._stats.frames_dropped,
            alerts_total=self._stats.alerts_total,
            started_at_monotonic=self._stats.started_at_monotonic,
            last_frame_at_monotonic=self._stats.last_frame_at_monotonic,
            avg_stream_fps=self._stats.avg_stream_fps,
            last_error=self._stats.last_error,
        )

    def start(self) -> None:
        self._stats.started_at_monotonic = time.monotonic()
        self._thread.start()
        self._emit(
            {
                "type": "ready",
                "session_id": self.session_id,
                "mission_id": self.mission.mission_id,
                "source_kind": self.source_kind,
                "source_value": self.source_value,
                "nav_mode": str(self.nav_mode),
                "detector_name": self.detector_name,
                "fps": self.mission.fps,
            }
        )

    def request_stop(self) -> None:
        """Signal the loop to stop and close the source to unblock any read."""
        self._stop_event.set()
        try:
            self._source.close()
        except (RuntimeError, OSError, ValueError):  # pragma: no cover
            logger.exception(
                "auto-session %s: source.close() during stop failed",
                self.session_id,
            )

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout=timeout)

    def wait_done(self, timeout: float | None = None) -> bool:
        return self._done_event.wait(timeout=timeout)

    def subscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.add(subscriber)

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.remove(subscriber)

    # ── Thread body ───────────────────────────────────────────────

    def _run(self) -> None:
        last_emit_t = 0.0
        try:
            iterator = self._source.frames()
        except (RuntimeError, ValueError, OSError, TypeError) as error:
            logger.exception("auto-session %s: source.frames() failed", self.session_id)
            self._stats.last_error = str(error)
            self._emit(
                {"type": "error", "session_id": self.session_id, "message": str(error)}
            )
            self._finalize()
            return

        try:
            for frame_bgr, ts_sec, frame_id in iterator:
                if self._stop_event.is_set():
                    break
                self._stats.frames_consumed += 1
                now = time.monotonic()

                image_uri = f"session://{self.session_id}/{frame_id}"
                try:
                    outcome = self._service.ingest_frame(
                        mission_id=self.mission.mission_id,
                        frame_bgr=frame_bgr,
                        ts_sec=float(ts_sec),
                        frame_id=int(frame_id),
                        image_uri=image_uri,
                        detect_enabled=self._detect_enabled,
                    )
                except (RuntimeError, ValueError, TypeError) as error:
                    logger.exception(
                        "auto-session %s: ingest_frame failed at frame_id=%s",
                        self.session_id,
                        frame_id,
                    )
                    self._stats.last_error = str(error)
                    self._emit(
                        {
                            "type": "error",
                            "session_id": self.session_id,
                            "frame_id": int(frame_id),
                            "message": str(error),
                        }
                    )
                    continue

                self._stats.alerts_total += len(outcome.alerts)
                self._stats.last_frame_at_monotonic = now

                if self._save_video:
                    self._write_recording_frame(frame_bgr, outcome.detections)

                # Rate-limit emissions to ws_emit_max_fps.
                if (
                    self._emit_min_dt > 0.0
                    and last_emit_t > 0.0
                    and (now - last_emit_t) < self._emit_min_dt
                ):
                    self._stats.frames_dropped += 1
                    continue

                self._refresh_stream_stats(now)

                jpeg_b64 = self._encoder.encode(frame_bgr)
                event = self._build_frame_event(
                    frame_id=int(frame_id),
                    ts_sec=float(ts_sec),
                    jpeg_b64=jpeg_b64,
                    outcome=outcome,
                )
                self._stats.frames_emitted += 1
                elapsed = now - self._stats.started_at_monotonic
                if elapsed > 0.0:
                    self._stats.avg_stream_fps = self._stats.frames_emitted / elapsed
                last_emit_t = now
                self._emit(event)
        except (RuntimeError, ValueError, TypeError) as error:
            logger.exception("auto-session %s: loop crashed", self.session_id)
            self._stats.last_error = str(error)
            self._emit(
                {
                    "type": "error",
                    "session_id": self.session_id,
                    "message": str(error),
                }
            )
        finally:
            self._finalize()

    def _finalize(self) -> None:
        self._close_recording()
        try:
            self._source.close()
        except (RuntimeError, OSError, ValueError):  # pragma: no cover
            logger.exception("auto-session %s: source.close() failed", self.session_id)

        completed_frame_id = None
        last_id = self._stats.frames_consumed - 1
        if last_id >= 0:
            completed_frame_id = last_id
        try:
            self._service.complete_auto_mission(
                mission_id=self.mission.mission_id,
                completed_frame_id=completed_frame_id,
            )
        except (RuntimeError, ValueError, TypeError) as error:
            logger.exception(
                "auto-session %s: complete_auto_mission failed",
                self.session_id,
            )
            self._stats.last_error = str(error)
            self._emit(
                {
                    "type": "error",
                    "session_id": self.session_id,
                    "message": f"complete failed: {error}",
                }
            )

        report: Mapping[str, object] | None = None
        try:
            report = self._service.get_auto_mission_report(self.mission.mission_id)
        except (RuntimeError, ValueError, TypeError):  # pragma: no cover
            report = None

        self._emit(
            {
                "type": "done",
                "session_id": self.session_id,
                "mission_id": self.mission.mission_id,
                "frames_consumed": self._stats.frames_consumed,
                "frames_emitted": self._stats.frames_emitted,
                "frames_dropped": self._stats.frames_dropped,
                "alerts_total": self._stats.alerts_total,
                "avg_stream_fps": self._stats.avg_stream_fps,
                "report": dict(report) if report is not None else None,
                "error": self._stats.last_error,
            }
        )
        self._done_event.set()
        try:
            self._on_finished(self)
        except (RuntimeError, ValueError, TypeError):  # pragma: no cover
            logger.exception(
                "auto-session %s: on_finished callback raised", self.session_id
            )

    def _build_frame_event(
        self,
        *,
        frame_id: int,
        ts_sec: float,
        jpeg_b64: str | None,
        outcome: AutoFrameOutcome,
    ) -> dict[str, object]:
        point_payload: dict[str, object] | None = None
        if outcome.trajectory_point is not None:
            point = outcome.trajectory_point
            point_payload = {
                "seq": point.seq,
                "ts_sec": point.ts_sec,
                "frame_id": point.frame_id,
                "x": point.x,
                "y": point.y,
                "z": point.z,
                "source": str(point.source),
            }
        detections = [
            {
                "bbox": list(det.bbox),
                "score": det.score,
                "label": det.label,
            }
            for det in outcome.detections
        ]
        alerts = [
            {
                "alert_id": alert.alert_id,
                "frame_id": alert.frame_id,
                "ts_sec": alert.ts_sec,
                "people_detected": alert.people_detected,
                "score": alert.primary_detection.score,
                "label": alert.primary_detection.label,
                "image_uri": alert.image_uri,
            }
            for alert in outcome.alerts
        ]
        person_count = sum(1 for d in outcome.detections if d.label == "person")
        event: dict[str, object] = {
            "type": "frame",
            "session_id": self.session_id,
            "mission_id": self.mission.mission_id,
            "frame_id": frame_id,
            "ts_sec": ts_sec,
            "frame_jpeg_b64": jpeg_b64,
            "person_count": person_count,
            "detections": detections,
            "trajectory_point": point_payload,
            "alerts": alerts,
            "stats": {
                "frames_consumed": self._stats.frames_consumed,
                "frames_emitted": self._stats.frames_emitted,
                "frames_dropped": self._stats.frames_dropped,
                "alerts_total": self._stats.alerts_total,
                "avg_stream_fps": self._stats.avg_stream_fps,
            },
        }
        if self._stream_stats_cache:
            event["stream_stats"] = dict(self._stream_stats_cache)
        return event

    # ── Stream-side stats (RPi potok channel) ─────────────────────

    def _refresh_stream_stats(self, now: float) -> None:
        """Poll the source's ``session_stats()`` at most every ~2s."""
        if self._stream_stats_fn is None:
            return
        if now - self._last_stream_stats_poll < 2.0:
            return
        self._last_stream_stats_poll = now
        try:
            payload = self._stream_stats_fn()
        except (RuntimeError, ValueError, OSError, TypeError):
            return
        if isinstance(payload, Mapping):
            self._stream_stats_cache = dict(payload)

    # ── Recording (save_video toggle) ─────────────────────────────

    def _write_recording_frame(
        self, frame_bgr: object, detections: Sequence[object]
    ) -> None:
        """Overlay bboxes and append frame to the per-session video file."""
        try:
            import cv2  # noqa: WPS433
        except ImportError:  # pragma: no cover
            return
        frame = cast(Any, frame_bgr)
        try:
            height, width = frame.shape[:2]
        except (AttributeError, TypeError, ValueError):  # pragma: no cover
            return

        if self._video_writer is None:
            import os
            import tempfile

            out_dir = self._save_video_dir or tempfile.gettempdir()
            os.makedirs(out_dir, exist_ok=True)
            filename = f"auto_{self.mission.mission_id}.mp4"
            path = os.path.join(out_dir, filename)
            fourcc = int(cv2.VideoWriter.fourcc(*"mp4v"))
            fps = self.mission.fps if self.mission.fps > 0 else 10.0
            writer = cv2.VideoWriter(
                path, fourcc, float(fps), (int(width), int(height))
            )
            if not writer.isOpened():
                logger.warning(
                    "auto-session %s: cv2.VideoWriter failed to open at %s",
                    self.session_id,
                    path,
                )
                return
            self._video_writer = writer
            self._video_writer_path = path
            logger.info(
                "auto-session %s: recording started at %s (%dx%d @ %.1ffps)",
                self.session_id,
                path,
                width,
                height,
                fps,
            )

        annotated = frame
        if detections:
            annotated = frame.copy()
            for det in detections:
                bbox = getattr(det, "bbox", None)
                if not bbox or len(bbox) < 4:
                    continue
                x1, y1, x2, y2 = (int(float(v)) for v in bbox[:4])
                cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)
                label = getattr(det, "label", "") or ""
                score = float(getattr(det, "score", 0.0))
                caption = f"{label} {score:.2f}" if label else f"{score:.2f}"
                cv2.putText(
                    annotated,
                    caption,
                    (x1, max(0, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (0, 255, 0),
                    1,
                    cv2.LINE_AA,
                )

        try:
            self._video_writer.write(annotated)
        except (RuntimeError, ValueError):  # pragma: no cover
            logger.exception(
                "auto-session %s: VideoWriter.write failed", self.session_id
            )

    def _close_recording(self) -> None:
        if self._video_writer is None:
            return
        try:
            self._video_writer.release()
        except (RuntimeError, ValueError):  # pragma: no cover
            pass
        logger.info(
            "auto-session %s: recording saved to %s",
            self.session_id,
            self._video_writer_path,
        )
        self._video_writer = None

    def _emit(self, event: Mapping[str, object]) -> None:
        for subscriber in self._subscribers.snapshot():
            try:
                subscriber(event)
            except (RuntimeError, ValueError, TypeError):  # pragma: no cover
                logger.exception("auto-session %s: subscriber raised", self.session_id)


class AutoSessionManager:
    """Registry + lifecycle of automatic-mode sessions (one at a time)."""

    def __init__(
        self,
        *,
        service: AutoMissionService,
        source_factory: VideoSourceFactory | None = None,
        ws_jpeg_quality: int = 55,
        ws_max_width: int = 640,
        ws_emit_max_fps: float = 8.0,
        save_video_dir: str | None = None,
    ) -> None:
        self._service = service
        self._source_factory = source_factory
        self._encoder = _JpegEncoder(max_width=ws_max_width, quality=ws_jpeg_quality)
        self._emit_max_fps = float(ws_emit_max_fps)
        self._save_video_dir = save_video_dir
        self._lock = threading.Lock()
        self._active: AutoSession | None = None

    # ── Queries ───────────────────────────────────────────────────

    def get_active(self) -> AutoSession | None:
        with self._lock:
            return self._active

    def require(self, session_id: str) -> AutoSession:
        session = self.get_active()
        if session is None or session.session_id != session_id:
            raise LookupError(f"session not found: {session_id}")
        return session

    # ── Commands ──────────────────────────────────────────────────

    def build_source(
        self,
        *,
        source_kind: str,
        source_value: str,
        fps: float | None,
        rpi_mission_id: str = "",
        demo_loop: bool = False,
    ) -> tuple[VideoFramePort, str, float]:
        """Resolve a ``(kind, value)`` pair into a ``VideoFramePort``.

        Returns ``(port, canonical_value, effective_fps)``. When the
        caller passes ``fps=None`` for a local video file, the source
        reports the container's native FPS — used both for navigation
        tuning and as the mission's recorded FPS.
        """
        if self._source_factory is None:
            raise RuntimeError("AutoSessionManager: no source_factory injected")
        return self._source_factory(
            source_kind, source_value, fps, rpi_mission_id, demo_loop
        )

    def start_session(
        self,
        *,
        request: StartSessionRequest,
    ) -> AutoSession:
        with self._lock:
            if self._active is not None and self._active.is_alive:
                raise RuntimeError(
                    "another automatic session is already running: "
                    f"{self._active.session_id}"
                )

            mission = self._service.start_auto_mission(
                source_name=f"{request.source_kind}:{request.source_value}",
                total_frames=int(request.total_frames),
                fps=float(request.fps),
                nav_mode=request.nav_mode,
                detector_name=request.detector_name,
                config_json=request.config_json,
            )
            session = AutoSession(
                init=AutoSessionInit(
                    session_id=str(uuid.uuid4()),
                    mission=mission,
                    source=request.source,
                    source_kind=request.source_kind,
                    source_value=request.source_value,
                    nav_mode=request.nav_mode,
                    detector_name=request.detector_name,
                    detect_enabled=request.detect_enabled,
                    save_video=request.save_video,
                    save_video_dir=self._save_video_dir,
                ),
                service=self._service,
                encoder=self._encoder,
                emit_max_fps=self._emit_max_fps,
                on_finished=self._on_session_finished,
            )
            self._active = session
        session.start()
        return session

    def stop_session(self, session_id: str, *, timeout: float = 10.0) -> AutoSession:
        session = self.require(session_id)
        session.request_stop()
        session.wait_done(timeout=timeout)
        session.join(timeout=1.0)
        return session

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """Stop any active session — used for graceful server shutdown."""
        session = self.get_active()
        if session is None:
            return
        session.request_stop()
        session.wait_done(timeout=timeout)
        session.join(timeout=1.0)

    # ── Bookkeeping ───────────────────────────────────────────────

    def _on_session_finished(self, session: AutoSession) -> None:
        with self._lock:
            if self._active is session:
                self._active = None


__all__ = [
    "AutoSession",
    "AutoSessionInfo",
    "AutoSessionManager",
    "AutoSessionStats",
    "Subscriber",
]
