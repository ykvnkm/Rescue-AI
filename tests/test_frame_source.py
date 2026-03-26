"""Unit tests for FrameSourceService."""

from __future__ import annotations

from pathlib import Path

from rescue_ai.application.frame_source import FrameSourceService, TimestampInputs


def test_list_frame_files_filters_images(tmp_path: Path) -> None:
    (tmp_path / "frame_001.jpg").touch()
    (tmp_path / "frame_002.png").touch()
    (tmp_path / "readme.txt").touch()
    (tmp_path / "frame_003.jpeg").touch()

    service = FrameSourceService()
    files = service.list_frame_files(tmp_path)
    assert len(files) == 3
    assert all(f.suffix.lower() in {".jpg", ".png", ".jpeg"} for f in files)


def test_list_frame_files_sorted(tmp_path: Path) -> None:
    (tmp_path / "b.jpg").touch()
    (tmp_path / "a.jpg").touch()

    service = FrameSourceService()
    files = service.list_frame_files(tmp_path)
    assert files[0].name == "a.jpg"
    assert files[1].name == "b.jpg"


def test_compute_ts_sec_by_frame_number() -> None:
    service = FrameSourceService()
    ts = service.compute_ts_sec(
        TimestampInputs(
            idx=0,
            frame_path=Path("frame_10.jpg"),
            fps=10.0,
            base_frame_num=0,
            prev_ts_sec=0.0,
        )
    )
    assert ts == 1.0


def test_compute_ts_sec_fallback_when_fps_zero() -> None:
    service = FrameSourceService()
    ts = service.compute_ts_sec(
        TimestampInputs(
            idx=3,
            frame_path=Path("frame_001.jpg"),
            fps=0.0,
            base_frame_num=None,
            prev_ts_sec=0.0,
        )
    )
    assert ts == 1.5  # 3 * 0.5


def test_compute_ts_sec_monotonicity() -> None:
    service = FrameSourceService()
    ts = service.compute_ts_sec(
        TimestampInputs(
            idx=0,
            frame_path=Path("frame_0.jpg"),
            fps=10.0,
            base_frame_num=0,
            prev_ts_sec=5.0,
        )
    )
    assert ts == 5.1  # prev_ts + dt because computed ts < prev


def test_extract_frame_number_underscore() -> None:
    service = FrameSourceService()
    assert service.extract_frame_number(Path("frame_42.jpg")) == 42


def test_extract_frame_number_trailing_digits() -> None:
    service = FrameSourceService()
    assert service.extract_frame_number(Path("img0007.jpg")) == 7


def test_extract_frame_number_no_digits() -> None:
    service = FrameSourceService()
    assert service.extract_frame_number(Path("nodigits.jpg")) is None
