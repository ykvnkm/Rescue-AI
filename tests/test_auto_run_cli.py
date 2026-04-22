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


def test_build_source_accepts_folder_scheme(tmp_path: Path) -> None:
    _write_dummy_frame(tmp_path, "a.jpg")
    _write_dummy_frame(tmp_path, "b.jpg")

    frames, source_name = auto_run._build_source(
        f"folder://{tmp_path}", fps_override=4.0
    )
    collected = list(frames)
    assert len(collected) == 2
    assert source_name.endswith(tmp_path.name)


def test_build_source_detects_directory_as_folder(tmp_path: Path) -> None:
    _write_dummy_frame(tmp_path, "a.jpg")

    frames, source_name = auto_run._build_source(str(tmp_path), fps_override=2.0)
    assert list(frames)  # yields at least one frame
    assert source_name == tmp_path.as_posix()


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
