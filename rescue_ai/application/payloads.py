"""Frame payload builders and detection serializers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from rescue_ai.domain.ports import DetectionPayload, FramePublishPayload


def build_frame_payload(
    frame_id: int,
    ts_sec: float,
    frame_path: Path,
    gt_boxes: list[tuple[float, float, float, float]],
    detections: list[DetectionPayload],
) -> FramePublishPayload:
    return {
        "frame_id": frame_id,
        "ts_sec": ts_sec,
        "image_uri": str(frame_path),
        "gt_person_present": bool(gt_boxes),
        "gt_episode_id": None,
        "detections": detections,
    }


def serialize_detections(
    detections: Sequence[Any],
    min_detections_per_frame: int,
) -> list[DetectionPayload]:
    payload_detections: list[DetectionPayload] = []
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
                    "label": getattr(item, "label", "person"),
                    "model_name": "yolov8n_baseline_multiscale",
                    "explanation": "yolo-frame-inference",
                }
            )
    return payload_detections
