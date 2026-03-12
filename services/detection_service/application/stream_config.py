from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from services.detection_service.application.annotation_index import (
    AnnotationIndex,
    build_annotation_index,
)
from services.detection_service.application.frame_source import FrameSourceService
from services.detection_service.domain.models import InferenceConfig, StreamContract


@dataclass
class StreamConfig:
    """Resolved runtime configuration for one mission stream."""

    mission_id: str
    frame_files: list[Path]
    fps: float
    api_base: str
    annotations: AnnotationIndex
    inference: InferenceConfig
    min_detections_per_frame: int


class StreamOptions(TypedDict):
    """External options accepted when creating stream configuration."""

    frames_dir: str
    annotations_path: str | None
    fps: float
    api_base: str


def build_stream_config(
    mission_id: str,
    options: StreamOptions,
    contract: StreamContract,
    frame_source: FrameSourceService | None = None,
) -> StreamConfig:
    source = frame_source or FrameSourceService()

    frames_path = Path(options["frames_dir"])
    if not frames_path.exists() or not frames_path.is_dir():
        raise ValueError(f"frames dir not found: {frames_path}")

    frame_files = source.list_frame_files(frames_path)
    if not frame_files:
        raise ValueError("no frames found")

    annotations = build_annotation_index(
        frames_dir=frames_path,
        explicit_path=options["annotations_path"],
    )

    fps = options["fps"]
    if fps <= 0:
        fps = contract.dataset_fps

    return StreamConfig(
        mission_id=mission_id,
        frame_files=frame_files,
        fps=fps,
        api_base=options["api_base"],
        annotations=annotations,
        inference=contract.inference,
        min_detections_per_frame=contract.min_detections_per_frame,
    )
