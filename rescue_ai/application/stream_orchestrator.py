"""Threaded stream orchestrator for real-time inference."""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError

from rescue_ai.application.frame_source import FrameSourceService, TimestampInputs
from rescue_ai.application.payloads import build_frame_payload, serialize_detections
from rescue_ai.domain.ports import DetectorPort, FramePublisherPort
from rescue_ai.domain.value_objects import InferenceConfig


class AnnotationIndexPort(Protocol):
    """Minimal annotation index API required by stream orchestration."""

    def get_gt_boxes(self, frame_path: Path) -> list[tuple[float, float, float, float]]:
        """Return GT boxes for the frame if available."""


@dataclass
class StreamConfig:
    """Resolved runtime configuration for one mission stream."""

    mission_id: str
    frame_files: list[Path]
    fps: float
    api_base: str
    annotations: AnnotationIndexPort
    inference: InferenceConfig
    min_detections_per_frame: int


@dataclass
class StreamState:
    """State snapshot of a running mission stream."""

    mission_id: str
    running: bool
    processed_frames: int
    total_frames: int
    last_frame_name: str | None
    error: str | None
    stop_requested: bool = False


DetectorFactory = Callable[[InferenceConfig], DetectorPort]


class _Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, StreamState] = {}
        self._stop_flags: dict[str, bool] = {}

    def get(self, mission_id: str) -> StreamState | None:
        """Return a copy of the stream state for the given mission."""
        with self._lock:
            state = self._states.get(mission_id)
            if state is None:
                return None
            return StreamState(**asdict(state))

    def set(self, state: StreamState) -> None:
        """Store or update the stream state for a mission."""
        with self._lock:
            self._states[state.mission_id] = state

    def set_stop(self, mission_id: str, value: bool) -> None:
        """Set the stop flag for a mission."""
        with self._lock:
            self._stop_flags[mission_id] = value

    def should_stop(self, mission_id: str) -> bool:
        """Return True if a stop has been requested for the mission."""
        with self._lock:
            return bool(self._stop_flags.get(mission_id, False))


class StreamOrchestrator:
    """Coordinates stream execution using abstract ports."""

    def __init__(
        self,
        detector_factory: DetectorFactory,
        frame_publisher: FramePublisherPort,
        frame_source: FrameSourceService | None = None,
    ) -> None:
        self._detector_factory = detector_factory
        self._frame_publisher = frame_publisher
        self._frame_source = frame_source or FrameSourceService()
        self._registry = _Registry()

    def get_stream_state(self, mission_id: str) -> StreamState | None:
        """Return the current state of a mission stream."""
        return self._registry.get(mission_id)

    def set_detector_factory(self, detector_factory: DetectorFactory) -> None:
        """Override detector factory for runtime/test wiring."""
        self._detector_factory = detector_factory

    def start_stream(self, config: StreamConfig) -> StreamState:
        """Initialize the detector and launch a background stream thread."""
        existing = self._registry.get(config.mission_id)
        if existing is not None and existing.running:
            raise ValueError("Stream already running for mission")

        detector = self._detector_factory(config.inference)
        detector.warmup()

        state = StreamState(
            mission_id=config.mission_id,
            running=True,
            processed_frames=0,
            total_frames=len(config.frame_files),
            last_frame_name=None,
            error=None,
            stop_requested=False,
        )
        self._registry.set(state)
        self._registry.set_stop(config.mission_id, False)

        thread = threading.Thread(
            target=self._run_stream,
            args=(config, detector),
            daemon=True,
        )
        thread.start()
        return state

    def stop_stream(self, mission_id: str) -> StreamState | None:
        """Request a running stream to stop gracefully."""
        current = self._registry.get(mission_id)
        if current is None:
            return None
        self._registry.set_stop(mission_id, True)
        current.stop_requested = True
        self._registry.set(current)
        return current

    def wait_stream_stopped(
        self,
        mission_id: str,
        timeout_sec: float = 3.0,
    ) -> StreamState | None:
        """Block until the stream stops or the timeout expires."""
        deadline = time.time() + max(0.1, timeout_sec)
        state = self._registry.get(mission_id)
        while time.time() < deadline:
            state = self._registry.get(mission_id)
            if state is None or not state.running:
                return state
            time.sleep(0.05)
        return self._registry.get(mission_id)

    def _run_stream(self, config: StreamConfig, detector: DetectorPort) -> None:
        dt = 1.0 / config.fps if config.fps > 0 else 0.5
        try:
            base_frame_num = self._frame_source.extract_frame_number(
                config.frame_files[0]
            )
            prev_ts_sec = -dt

            for idx, frame_path in enumerate(config.frame_files):
                if self._registry.should_stop(config.mission_id):
                    self._mark_stop_requested(config.mission_id)
                    return
                current = self._registry.get(config.mission_id)
                if current is None:
                    return
                if current.stop_requested:
                    current.running = False
                    self._registry.set(current)
                    return

                gt_boxes = config.annotations.get_gt_boxes(frame_path)
                detections = detector.detect(str(frame_path))
                payload_detections = serialize_detections(
                    detections=detections,
                    min_detections_per_frame=config.min_detections_per_frame,
                )
                ts_sec = round(
                    self._frame_source.compute_ts_sec(
                        TimestampInputs(
                            idx=idx,
                            frame_path=frame_path,
                            fps=config.fps,
                            base_frame_num=base_frame_num,
                            prev_ts_sec=prev_ts_sec,
                        )
                    ),
                    3,
                )
                payload = build_frame_payload(
                    frame_id=idx,
                    ts_sec=ts_sec,
                    frame_path=frame_path,
                    gt_boxes=gt_boxes,
                    detections=payload_detections,
                )
                prev_ts_sec = ts_sec
                self._frame_publisher.publish(
                    mission_id=config.mission_id,
                    api_base=config.api_base,
                    payload=payload,
                )

                current = self._registry.get(config.mission_id)
                if current is None:
                    return
                current.processed_frames = idx + 1
                current.last_frame_name = frame_path.name
                self._registry.set(current)
                if self._registry.should_stop(config.mission_id):
                    self._mark_stop_requested(config.mission_id)
                    return
                time.sleep(dt)

            current = self._registry.get(config.mission_id)
            if current is not None:
                current.running = False
                self._registry.set(current)
        except (HTTPError, URLError, OSError, ValueError, RuntimeError) as error:
            current = self._registry.get(config.mission_id)
            if current is None:
                return
            current.running = False
            current.error = str(error)
            self._registry.set(current)

    def _mark_stop_requested(self, mission_id: str) -> None:
        current = self._registry.get(mission_id)
        if current is None:
            return
        current.running = False
        current.stop_requested = True
        self._registry.set(current)


def frame_name(frame_path: Path) -> str:
    """Extract the file name from a frame path."""
    return frame_path.name
