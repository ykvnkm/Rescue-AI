"""Factory for auto-session video sources in online composition root."""

from __future__ import annotations

from pathlib import Path

from rescue_ai.config import get_settings
from rescue_ai.infrastructure.rpi_client import RpiClient
from rescue_ai.infrastructure.video import (
    FileVideoSource,
    FolderFramesSource,
    RemoteRpiVideoSource,
    RTSPVideoSource,
)


def auto_video_source_factory(
    source_kind: str,
    source_value: str,
    fps: float,
    rpi_mission_id: str = "",
    demo_loop: bool = False,
) -> tuple[object, str]:
    """Map ``(kind, value)`` to a video source; supports local and stream channels."""
    if rpi_mission_id:
        if source_kind not in ("video", "frames"):
            raise ValueError(
                f"stream channel does not support source_kind={source_kind!r}"
            )
        settings = get_settings()
        if not settings.rpi.base_url.strip():
            raise RuntimeError("RPi base_url not configured")
        rpi_client = RpiClient(settings.rpi)
        source = RemoteRpiVideoSource(
            rpi_client=rpi_client,
            mission_id=rpi_mission_id,
            target_fps=fps,
        )
        resolved = f"rpi:{rpi_mission_id}:{source.session_id}"
        return source, resolved

    if source_kind == "video":
        path = Path(source_value)
        if not path.is_file():
            raise FileNotFoundError(f"video file not found: {source_value}")
        return (
            FileVideoSource(str(path), fps_override=fps, loop=demo_loop),
            str(path),
        )
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
