"""Tests for the ``rescue_ai.interfaces.cli.auto_run`` composition root.

Only the URI → VideoFramePort mapping and argument parser are exercised
here; the full wiring requires postgres + S3 + detector models and is
covered by integration tests elsewhere.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from rescue_ai.interfaces.cli import auto_run


def _write_dummy_frame(directory: Path, name: str) -> None:
    frame = np.zeros((16, 16, 3), dtype=np.uint8)
    cv2.imwrite(str(directory / name), frame)


def _write_dummy_video(path: Path, *, fps: float) -> None:
    frame = np.zeros((16, 16, 3), dtype=np.uint8)
    fourcc = int(cv2.VideoWriter.fourcc(*"mp4v"))
    writer = cv2.VideoWriter(str(path), fourcc, fps, (16, 16))
    assert writer.isOpened()
    writer.write(frame)
    writer.write(frame)
    writer.release()


def test_build_source_accepts_folder_scheme(tmp_path: Path) -> None:
    _write_dummy_frame(tmp_path, "a.jpg")
    _write_dummy_frame(tmp_path, "b.jpg")

    frames, source_name, fps = auto_run._build_source(
        f"folder://{tmp_path}", fps_override=4.0
    )
    collected = list(frames)
    assert len(collected) == 2
    assert source_name.endswith(tmp_path.name)
    assert fps == pytest.approx(4.0)


def test_build_source_detects_directory_as_folder(tmp_path: Path) -> None:
    _write_dummy_frame(tmp_path, "a.jpg")

    frames, source_name, fps = auto_run._build_source(str(tmp_path), fps_override=2.0)
    assert list(frames)  # yields at least one frame
    assert source_name == tmp_path.as_posix()
    assert fps == pytest.approx(2.0)


def test_build_source_uses_reported_video_fps(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    _write_dummy_video(video, fps=11.0)

    frames, source_name, fps = auto_run._build_source(
        str(video), fps_override=None, default_fps=6.0
    )

    assert list(frames)
    assert source_name == video.as_posix()
    assert fps == pytest.approx(11.0)


def test_build_source_rejects_unknown_scheme() -> None:
    with pytest.raises(FileNotFoundError):
        auto_run._build_source("file:///does/not/exist.mp4", fps_override=None)


def test_parse_args_defaults() -> None:
    args = auto_run._parse_args(["--source", "rtsp://cam/stream"])
    assert args.source == "rtsp://cam/stream"
    assert args.config is None
    assert args.nav_mode == "auto"
    assert args.max_frames is None


def test_parse_args_overrides() -> None:
    args = auto_run._parse_args(
        [
            "--source",
            "file:///tmp/v.mp4",
            "--config",
            "configs/custom.yaml",
            "--fps",
            "5.0",
            "--nav-mode",
            "marker",
            "--max-frames",
            "100",
        ]
    )
    assert args.config == "configs/custom.yaml"
    assert args.fps == 5.0
    assert args.nav_mode == "marker"
    assert args.max_frames == 100
