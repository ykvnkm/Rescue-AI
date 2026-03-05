"""Background frame stream runner used by UI-driven pilot simulation."""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TypedDict
from urllib import request
from urllib.error import HTTPError, URLError

from services.detection_service.infrastructure.runtime_contract import (
    InferenceConfig,
    load_stream_contract,
)
from services.detection_service.infrastructure.yolo_detector import YoloDetector


@dataclass
class StreamState:
    """Current state of background stream replay."""

    mission_id: str
    running: bool
    processed_frames: int
    total_frames: int
    last_frame_name: str | None
    error: str | None
    stop_requested: bool = False


@dataclass
class StreamConfig:
    """Configuration for stream replay."""

    mission_id: str
    frame_files: list[Path]
    fps: float
    api_base: str
    annotations: "AnnotationIndex"
    inference: InferenceConfig
    min_detections_per_frame: int


class StreamOptions(TypedDict):
    """Options used to construct stream config."""

    frames_dir: str
    annotations_path: str | None
    fps: float
    api_base: str


class _Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, StreamState] = {}
        self._stop_flags: dict[str, bool] = {}

    def get(self, mission_id: str) -> StreamState | None:
        with self._lock:
            state = self._states.get(mission_id)
            if state is None:
                return None
            return StreamState(**asdict(state))

    def set(self, state: StreamState) -> None:
        with self._lock:
            self._states[state.mission_id] = state

    def set_stop(self, mission_id: str, value: bool) -> None:
        with self._lock:
            self._stop_flags[mission_id] = value

    def should_stop(self, mission_id: str) -> bool:
        with self._lock:
            return bool(self._stop_flags.get(mission_id, False))


_registry = _Registry()


def get_stream_state(mission_id: str) -> StreamState | None:
    return _registry.get(mission_id)


def build_stream_config(
    mission_id: str,
    options: StreamOptions,
) -> StreamConfig:
    """Validate input and create stream config."""
    contract = load_stream_contract()

    frames_path = Path(options["frames_dir"])
    if not frames_path.exists() or not frames_path.is_dir():
        raise ValueError(f"frames dir not found: {frames_path}")

    frame_files = sorted(
        path
        for path in frames_path.iterdir()
        if path.suffix.lower() in {".png", ".jpg", ".jpeg"}
    )
    if not frame_files:
        raise ValueError("no frames found")

    annotations = build_annotation_index(
        frames_dir=frames_path,
        explicit_path=options["annotations_path"],
    )

    fps = options["fps"]
    if fps <= 0:
        fps = contract.dataset_fps

    inference = InferenceConfig(
        model_url=contract.inference.model_url,
        device=contract.inference.device,
        imgsz=contract.inference.imgsz,
        nms_iou=contract.inference.nms_iou,
        max_det=contract.inference.max_det,
        confidence_threshold=contract.inference.confidence_threshold,
    )

    return StreamConfig(
        mission_id=mission_id,
        frame_files=frame_files,
        fps=fps,
        api_base=options["api_base"],
        annotations=annotations,
        inference=inference,
        min_detections_per_frame=contract.min_detections_per_frame,
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
        stop_requested=False,
    )
    _registry.set(state)
    _registry.set_stop(config.mission_id, False)

    thread = threading.Thread(target=_run_stream, args=(config,), daemon=True)
    thread.start()
    return state


def _run_stream(config: StreamConfig) -> None:
    dt = 1.0 / config.fps if config.fps > 0 else 0.5
    try:
        detector = YoloDetector(config.inference)
        base_frame_num = _extract_frame_number(config.frame_files[0])
        prev_ts_sec = -dt

        for idx, frame_path in enumerate(config.frame_files):
            if _registry.should_stop(config.mission_id):
                current = _registry.get(config.mission_id)
                if current is not None:
                    current.running = False
                    current.stop_requested = True
                    _registry.set(current)
                return
            current = _registry.get(config.mission_id)
            if current is None:
                return
            if current.stop_requested:
                current.running = False
                _registry.set(current)
                return

            gt_boxes = config.annotations.get_gt_boxes(frame_path)
            detections = detector.predict(frame_path)
            payload_detections = _serialize_detections(
                detections=detections,
                min_detections_per_frame=config.min_detections_per_frame,
            )
            ts_sec = round(
                _compute_frame_ts_sec(
                    idx=idx,
                    frame_path=frame_path,
                    fps=config.fps,
                    base_frame_num=base_frame_num,
                    prev_ts_sec=prev_ts_sec,
                ),
                3,
            )
            payload = _build_frame_payload(
                frame_id=idx,
                ts_sec=ts_sec,
                frame_path=frame_path,
                gt_boxes=gt_boxes,
                detections=payload_detections,
            )
            prev_ts_sec = ts_sec
            _post_json(
                f"{config.api_base}/v1/missions/{config.mission_id}/frames",
                payload,
            )

            current = _registry.get(config.mission_id)
            if current is None:
                return
            current.processed_frames = idx + 1
            current.last_frame_name = frame_path.name
            _registry.set(current)
            if _registry.should_stop(config.mission_id):
                current.running = False
                current.stop_requested = True
                _registry.set(current)
                return
            time.sleep(dt)

        current = _registry.get(config.mission_id)
        if current is not None:
            current.running = False
            _registry.set(current)
    except (HTTPError, URLError, OSError, ValueError, RuntimeError) as error:
        current = _registry.get(config.mission_id)
        if current is None:
            return
        current.running = False
        current.error = str(error)
        _registry.set(current)


def stop_stream(mission_id: str) -> StreamState | None:
    current = _registry.get(mission_id)
    if current is None:
        return None
    _registry.set_stop(mission_id, True)
    current.stop_requested = True
    _registry.set(current)
    return current


def wait_stream_stopped(
    mission_id: str, timeout_sec: float = 3.0
) -> StreamState | None:
    deadline = time.time() + max(0.1, timeout_sec)
    state = _registry.get(mission_id)
    while time.time() < deadline:
        state = _registry.get(mission_id)
        if state is None or not state.running:
            return state
        time.sleep(0.05)
    return _registry.get(mission_id)


def _build_frame_payload(
    frame_id: int,
    ts_sec: float,
    frame_path: Path,
    gt_boxes: list[tuple[float, float, float, float]],
    detections: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "frame_id": frame_id,
        "ts_sec": ts_sec,
        "image_uri": str(frame_path),
        "gt_person_present": bool(gt_boxes),
        "gt_episode_id": None,
        "detections": detections,
    }


def _serialize_detections(
    detections: list[Any],
    min_detections_per_frame: int,
) -> list[dict[str, object]]:
    payload_detections: list[dict[str, object]] = []
    if len(detections) >= min_detections_per_frame:
        for item in detections:
            payload_detections.append(
                {
                    "bbox": [
                        float(item.bbox[0]),
                        float(item.bbox[1]),
                        float(item.bbox[2]),
                        float(item.bbox[3]),
                    ],
                    "score": float(item.score),
                    "label": "person",
                    "model_name": "yolov8n_baseline_multiscale",
                    "explanation": "yolo-frame-inference",
                }
            )
    return payload_detections


def _post_json(url: str, payload: dict[str, object]) -> dict[str, object]:
    req = request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _compute_frame_ts_sec(
    idx: int,
    frame_path: Path,
    fps: float,
    base_frame_num: int | None,
    prev_ts_sec: float,
) -> float:
    dt = 1.0 / fps if fps > 0 else 0.5
    if fps <= 0:
        return idx * dt

    frame_num = _extract_frame_number(frame_path)
    if frame_num is None or base_frame_num is None:
        ts_sec = idx * dt
    else:
        ts_sec = max((frame_num - base_frame_num) / fps, 0.0)

    if ts_sec < prev_ts_sec:
        ts_sec = prev_ts_sec + dt
    return ts_sec


def _extract_frame_number(frame_path: Path) -> int | None:
    stem = frame_path.stem
    parts = stem.split("_")
    if parts and parts[-1].isdigit():
        return int(parts[-1])
    match = re.search(r"(\d+)$", stem)
    if match is None:
        return None
    return int(match.group(1))


class AnnotationIndex:
    """Lookup object for GT boxes by frame path with COCO-safe matching."""

    def __init__(
        self,
        frames_dir: Path,
        gt_boxes_by_key: dict[str, list[tuple[float, float, float, float]]],
        gt_boxes_by_unique_basename: dict[str, list[tuple[float, float, float, float]]],
    ) -> None:
        self._frames_dir = frames_dir.resolve()
        self._gt_boxes_by_key = gt_boxes_by_key
        self._gt_boxes_by_unique_basename = gt_boxes_by_unique_basename

    def get_gt_boxes(self, frame_path: Path) -> list[tuple[float, float, float, float]]:
        for key in self._build_lookup_keys(frame_path):
            boxes = self._gt_boxes_by_key.get(key)
            if boxes is not None:
                return list(boxes)
        return list(self._gt_boxes_by_unique_basename.get(frame_path.name, []))

    def has_frame(self, frame_path: Path) -> bool:
        if frame_path.name in self._gt_boxes_by_unique_basename:
            return True
        for key in self._build_lookup_keys(frame_path):
            if key in self._gt_boxes_by_key:
                return True
        return False

    def _build_lookup_keys(self, frame_path: Path) -> list[str]:
        path = frame_path.resolve()
        keys: list[str] = []
        try:
            keys.append(_normalize_key(path.relative_to(self._frames_dir.parent)))
        except ValueError:
            pass
        try:
            keys.append(_normalize_key(path.relative_to(self._frames_dir)))
        except ValueError:
            pass
        keys.append(_normalize_key(Path(frame_path.name)))
        return keys


def build_annotation_index(
    frames_dir: Path, explicit_path: str | None
) -> AnnotationIndex:
    source = _resolve_annotations_source(frames_dir=frames_dir, explicit=explicit_path)
    return _build_from_coco_json(coco_path=source, frames_dir=frames_dir)


def _resolve_annotations_source(frames_dir: Path, explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise ValueError(f"annotations source not found: {path}")
        if path.is_file():
            if path.suffix.lower() != ".json":
                raise ValueError("annotations file must be COCO .json")
            return path

        json_files = sorted(
            item for item in path.iterdir() if item.suffix.lower() == ".json"
        )
        if not json_files:
            raise ValueError("annotations dir must contain COCO .json file")
        return json_files[0]

    candidates = [
        frames_dir / "annotations",
        frames_dir.parent / "annotations",
    ]
    for candidate in candidates:
        if not candidate.exists() or not candidate.is_dir():
            continue
        json_files = sorted(
            path for path in candidate.iterdir() if path.suffix.lower() == ".json"
        )
        if json_files:
            return json_files[0]
    raise ValueError(
        "COCO annotations not found. Expected JSON in <mission>/annotations"
    )


def _build_from_coco_json(coco_path: Path, frames_dir: Path) -> AnnotationIndex:
    payload = json.loads(coco_path.read_text(encoding="utf-8"))
    image_keys_by_id, image_basename_by_id, basename_count = _extract_image_maps(
        _get_payload_rows(payload, "images")
    )
    person_category_ids = _extract_person_category_ids(
        _get_payload_rows(payload, "categories")
    )
    gt_boxes_by_key = _build_gt_boxes_by_key(
        annotations=_get_payload_rows(payload, "annotations"),
        image_keys_by_id=image_keys_by_id,
        person_category_ids=person_category_ids,
    )
    gt_boxes_by_unique_basename = _build_unique_basename_box_map(
        image_basename_by_id=image_basename_by_id,
        basename_count=basename_count,
        image_keys_by_id=image_keys_by_id,
        gt_boxes_by_key=gt_boxes_by_key,
    )

    return AnnotationIndex(
        frames_dir=frames_dir,
        gt_boxes_by_key=gt_boxes_by_key,
        gt_boxes_by_unique_basename=gt_boxes_by_unique_basename,
    )


def _get_payload_rows(payload: Any, key: str) -> list[Any]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get(key, [])
    if not isinstance(rows, list):
        return []
    return rows


def _extract_image_maps(
    rows: list[Any],
) -> tuple[dict[int, list[str]], dict[int, str], dict[str, int]]:
    image_keys_by_id: dict[int, list[str]] = {}
    image_basename_by_id: dict[int, str] = {}
    basename_count: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        image_id = row.get("id")
        file_name = row.get("file_name")
        if isinstance(image_id, int) and isinstance(file_name, str):
            normalized = _normalize_key(Path(file_name))
            basename = Path(file_name).name
            keys = [normalized]
            tail = _normalize_without_images_prefix(normalized)
            if tail != normalized:
                keys.append(tail)
            image_keys_by_id[image_id] = keys
            image_basename_by_id[image_id] = basename
            basename_count[basename] = basename_count.get(basename, 0) + 1
    return image_keys_by_id, image_basename_by_id, basename_count


def _extract_person_category_ids(rows: list[Any]) -> set[int]:
    person_category_ids: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        category_id = row.get("id")
        category_name = row.get("name")
        name = str(category_name).strip().lower()
        if not isinstance(category_id, int):
            continue
        if name == "person" or "person" in name or "human" in name:
            person_category_ids.add(category_id)
    if not person_category_ids:
        person_category_ids = {1}
    return person_category_ids


def _append_coco_box(
    row: Any,
    gt_boxes_by_key: dict[str, list[tuple[float, float, float, float]]],
    image_keys_by_id: dict[int, list[str]],
    person_category_ids: set[int],
) -> None:
    if not isinstance(row, dict):
        return

    parsed = _parse_coco_annotation_row(row)
    if parsed is None:
        return

    image_id, category_id, bbox = parsed
    if person_category_ids and category_id not in person_category_ids:
        return

    image_keys = image_keys_by_id.get(image_id)
    if not image_keys:
        return

    converted = _xywh_to_xyxy(bbox)
    for key in image_keys:
        gt_boxes_by_key.setdefault(key, []).append(converted)


def _build_gt_boxes_by_key(
    annotations: list[Any],
    image_keys_by_id: dict[int, list[str]],
    person_category_ids: set[int],
) -> dict[str, list[tuple[float, float, float, float]]]:
    gt_boxes_by_key: dict[str, list[tuple[float, float, float, float]]] = {}
    for row in annotations:
        _append_coco_box(
            row=row,
            gt_boxes_by_key=gt_boxes_by_key,
            image_keys_by_id=image_keys_by_id,
            person_category_ids=person_category_ids,
        )
    return gt_boxes_by_key


def _build_unique_basename_box_map(
    image_basename_by_id: dict[int, str],
    basename_count: dict[str, int],
    image_keys_by_id: dict[int, list[str]],
    gt_boxes_by_key: dict[str, list[tuple[float, float, float, float]]],
) -> dict[str, list[tuple[float, float, float, float]]]:
    gt_boxes_by_unique_basename: dict[str, list[tuple[float, float, float, float]]] = {}
    for image_id, basename in image_basename_by_id.items():
        if basename_count.get(basename, 0) != 1:
            continue
        keys = image_keys_by_id.get(image_id, [])
        if not keys:
            continue
        gt_boxes_by_unique_basename[basename] = gt_boxes_by_key.get(keys[0], [])
    return gt_boxes_by_unique_basename


def _parse_coco_annotation_row(
    row: dict[Any, Any],
) -> tuple[int, int, list[float | int]] | None:
    image_id_raw = row.get("image_id")
    category_id_raw = row.get("category_id")
    bbox = row.get("bbox")
    if (
        not isinstance(image_id_raw, (int, str))
        or not isinstance(category_id_raw, (int, str))
        or not isinstance(bbox, list)
        or len(bbox) != 4
    ):
        return None
    try:
        image_id = int(image_id_raw)
        category_id = int(category_id_raw)
    except ValueError:
        return None
    return image_id, category_id, bbox


def _xywh_to_xyxy(bbox: list[float | int]) -> tuple[float, float, float, float]:
    x, y, w, h = bbox
    return float(x), float(y), float(x + w), float(y + h)


def _normalize_key(path: Path) -> str:
    return str(path).replace("\\", "/").strip("./")


def _normalize_without_images_prefix(path_key: str) -> str:
    if path_key.startswith("images/"):
        return path_key.removeprefix("images/")
    return path_key
