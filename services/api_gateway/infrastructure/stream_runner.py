"""Background frame stream runner used by UI-driven pilot simulation."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypedDict
from urllib import request
from urllib.error import HTTPError, URLError


@dataclass
class StreamState:
    """Current state of background stream replay."""

    mission_id: str
    running: bool
    processed_frames: int
    total_frames: int
    last_frame_name: str | None
    error: str | None


@dataclass
class StreamConfig:
    """Configuration for stream replay."""

    mission_id: str
    frame_files: list[Path]
    labels_path: Path | None
    fps: float
    high_score: float
    low_score: float
    api_base: str


class StreamOptions(TypedDict):
    """Options used to construct stream config."""

    frames_dir: str
    labels_dir: str | None
    fps: float
    high_score: float
    low_score: float
    api_base: str


class _Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, StreamState] = {}

    def get(self, mission_id: str) -> StreamState | None:
        with self._lock:
            state = self._states.get(mission_id)
            if state is None:
                return None
            return StreamState(**asdict(state))

    def set(self, state: StreamState) -> None:
        with self._lock:
            self._states[state.mission_id] = state


_registry = _Registry()


def get_stream_state(mission_id: str) -> StreamState | None:
    return _registry.get(mission_id)


def build_stream_config(
    mission_id: str,
    options: StreamOptions,
) -> StreamConfig:
    """Validate input and create stream config."""
    frames_dir = options["frames_dir"]
    labels_dir = options["labels_dir"]
    fps = options["fps"]
    high_score = options["high_score"]
    low_score = options["low_score"]
    api_base = options["api_base"]

    frames_path = Path(frames_dir)
    if not frames_path.exists():
        raise ValueError(f"frames dir not found: {frames_path}")

    labels_path = Path(labels_dir) if labels_dir else None
    if labels_path is not None and not labels_path.exists():
        raise ValueError(f"labels dir not found: {labels_path}")

    frame_files = sorted(
        path
        for path in frames_path.iterdir()
        if path.suffix.lower() in {".png", ".jpg", ".jpeg"}
    )
    if not frame_files:
        raise ValueError("no frames found")

    return StreamConfig(
        mission_id=mission_id,
        frame_files=frame_files,
        labels_path=labels_path,
        fps=fps,
        high_score=high_score,
        low_score=low_score,
        api_base=api_base,
    )


def start_stream(config: StreamConfig) -> StreamState:
    existing = _registry.get(config.mission_id)
    if existing is not None and existing.running:
        raise ValueError("Stream already running for mission")

    state = StreamState(
        mission_id=config.mission_id,
        running=True,
        processed_frames=0,
        total_frames=len(config.frame_files),
        last_frame_name=None,
        error=None,
    )
    _registry.set(state)

    thread = threading.Thread(target=_run_stream, args=(config,), daemon=True)
    thread.start()
    return state


def _run_stream(config: StreamConfig) -> None:
    dt = 1.0 / config.fps if config.fps > 0 else 0.5
    try:
        for idx, frame_path in enumerate(config.frame_files):
            gt_present = _has_ground_truth(
                frame_path=frame_path,
                labels_dir=config.labels_path,
            )
            score = config.high_score if gt_present else config.low_score
            payload = _build_frame_payload(
                frame_id=idx,
                ts_sec=round(idx * dt, 3),
                frame_path=frame_path,
                gt_present=gt_present,
                score=score,
            )
            _post_json(
                f"{config.api_base}/v1/missions/{config.mission_id}/frames", payload
            )

            current = _registry.get(config.mission_id)
            if current is None:
                return
            current.processed_frames = idx + 1
            current.last_frame_name = frame_path.name
            _registry.set(current)
            time.sleep(dt)

        current = _registry.get(config.mission_id)
        if current is not None:
            current.running = False
            _registry.set(current)
    except (HTTPError, URLError, OSError, ValueError) as error:
        current = _registry.get(config.mission_id)
        if current is None:
            return
        current.running = False
        current.error = str(error)
        _registry.set(current)


def _build_frame_payload(
    frame_id: int,
    ts_sec: float,
    frame_path: Path,
    gt_present: bool,
    score: float,
) -> dict[str, object]:
    detections: list[dict[str, object]] = []
    if gt_present:
        detections.append(
            {
                "bbox": [15.0, 15.0, 60.0, 60.0],
                "score": score,
                "label": "person",
                "model_name": "yolo8n",
                "explanation": "stream-runner",
            }
        )

    return {
        "frame_id": frame_id,
        "ts_sec": ts_sec,
        "image_uri": str(frame_path),
        "gt_person_present": gt_present,
        "gt_episode_id": None,
        "detections": detections,
    }


def _has_ground_truth(frame_path: Path, labels_dir: Path | None) -> bool:
    if labels_dir is None:
        label_path = frame_path.with_suffix(".txt")
    else:
        label_path = labels_dir / f"{frame_path.stem}.txt"
    if not label_path.exists():
        return False
    return label_path.read_text(encoding="utf-8").strip() != ""


def _post_json(url: str, payload: dict[str, object]) -> dict[str, object]:
    req = request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))
