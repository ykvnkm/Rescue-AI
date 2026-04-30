"""Composition-root factory for automatic session video sources."""

from __future__ import annotations

from pathlib import Path

from rescue_ai.infrastructure.video import (
    FileVideoSource,
    FolderFramesSource,
    RTSPVideoSource,
)


def auto_video_source_factory(
    source_kind: str,
    source_value: str,
    fps: float,
) -> tuple[object, str]:
    """Map ``(kind, value)`` to a concrete video source and canonical value."""
    if source_kind == "video":
        path = Path(source_value)
        if not path.is_file():
            raise FileNotFoundError(f"video file not found: {source_value}")
        return FileVideoSource(str(path), fps_override=fps), str(path)
    if source_kind == "frames":
        path = Path(source_value)
        if not path.is_dir():
            raise FileNotFoundError(f"frames directory not found: {source_value}")
        return FolderFramesSource(str(path), fps=fps), str(path)
    if source_kind == "rtsp":
        if not source_value:
            raise ValueError("rtsp url must be non-empty")
        return RTSPVideoSource(source_value), source_value
    raise ValueError(f"unknown source_kind: {source_kind}")
