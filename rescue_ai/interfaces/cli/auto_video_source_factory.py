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
    fps: float | None,
    rpi_mission_id: str = "",
    demo_loop: bool = False,
) -> tuple[object, str, float]:
    """Map ``(kind, value)`` to a video source; supports local and stream channels.

    Returns ``(port, canonical_value, effective_fps)``. ``effective_fps``
    is the FPS the caller should use downstream — for local video files
    it is the file's reported / overridden FPS (``FileVideoSource.fps``);
    for streams it equals the requested ``fps`` argument.
    """
    if rpi_mission_id:
        if source_kind not in ("video", "frames"):
            raise ValueError(
                f"stream channel does not support source_kind={source_kind!r}"
            )
        if fps is None or fps <= 0:
            raise ValueError("stream channel requires a positive fps")
        settings = get_settings()
        if not settings.rpi.base_url.strip():
            raise RuntimeError("RPi base_url not configured")
        # ADR-0007 §4: mTLS material lives in SecuritySettings and must
        # propagate to every outbound RPi call. The auto-mode stream
        # channel is the second egress point (operator side is wired in
        # online.py::DetectionStreamController._client) — keep them in
        # sync so tls_mode=mtls works regardless of which mode opens
        # the session.
        rpi_client = RpiClient(settings.rpi, security=settings.security)
        remote_source = RemoteRpiVideoSource(
            rpi_client=rpi_client,
            mission_id=rpi_mission_id,
            target_fps=fps,
        )
        resolved = f"rpi:{rpi_mission_id}:{remote_source.session_id}"
        return remote_source, resolved, float(fps)

    if source_kind == "video":
        path = Path(source_value)
        if not path.is_file():
            raise FileNotFoundError(f"video file not found: {source_value}")
        # When the caller did not pin an FPS, FileVideoSource falls back
        # to the file's reported metadata (or _DEFAULT_FPS). Surface the
        # resolved value so downstream tuning uses the real frame rate.
        file_source = FileVideoSource(
            str(path),
            fps_override=fps if fps and fps > 0 else None,
            loop=demo_loop,
        )
        return file_source, str(path), float(file_source.fps)
    if source_kind == "frames":
        path = Path(source_value)
        if not path.is_dir():
            raise FileNotFoundError(f"frames directory not found: {source_value}")
        if fps is None or fps <= 0:
            raise ValueError("frames source requires a positive fps")
        return FolderFramesSource(str(path), fps=fps), str(path), float(fps)
    if source_kind == "rtsp":
        if not source_value:
            raise ValueError("rtsp url must be non-empty")
        if fps is None or fps <= 0:
            raise ValueError("rtsp source requires a positive fps")
        return RTSPVideoSource(source_value), source_value, float(fps)
    raise ValueError(f"unknown source_kind: {source_kind}")
