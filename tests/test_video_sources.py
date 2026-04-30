"""Unit tests for file / folder / RTSP video sources (P1.3)."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import cv2
import numpy as np
import pytest

from rescue_ai.infrastructure.video import (
    FileVideoSource,
    FolderFramesSource,
    MjpegHTTPSource,
    RTSPVideoSource,
)

# ── Helpers ─────────────────────────────────────────────────────────


def _make_frame(fill: int) -> np.ndarray:
    frame = np.full((32, 48, 3), fill % 256, dtype=np.uint8)
    return frame


def _write_video(path: Path, n: int, fps: float = 15.0) -> None:
    fourcc = int(cv2.VideoWriter.fourcc(*"mp4v"))
    writer = cv2.VideoWriter(str(path), fourcc, fps, (48, 32))
    assert writer.isOpened(), "test VideoWriter failed to open"
    for i in range(n):
        writer.write(_make_frame(20 * i))
    writer.release()


# ── FileVideoSource ─────────────────────────────────────────────────


def test_file_source_rejects_missing_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        FileVideoSource(tmp_path / "nope.mp4")


def test_file_source_yields_sequential_ids_and_ts(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    _write_video(video, n=5, fps=10.0)

    src = FileVideoSource(video)
    items = list(src.frames())

    assert src.fps == pytest.approx(10.0)
    assert [fid for _, _, fid in items] == [0, 1, 2, 3, 4]
    ts = [t for _, t, _ in items]
    assert ts[0] == pytest.approx(0.0)
    assert ts[1] > ts[0]
    diffs = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    assert all(d == pytest.approx(diffs[0]) for d in diffs)


def test_file_source_fps_override(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    _write_video(video, n=3, fps=10.0)

    src = FileVideoSource(video, fps_override=25.0)
    items = list(src.frames())
    ts = [t for _, t, _ in items]

    assert src.fps == pytest.approx(25.0)
    assert ts[1] == pytest.approx(1.0 / 25.0)


# ── FolderFramesSource ──────────────────────────────────────────────


def test_folder_source_requires_existing_dir(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        FolderFramesSource(tmp_path / "missing", fps=10.0)


def test_folder_source_requires_non_empty(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError):
        FolderFramesSource(empty, fps=10.0)


def test_folder_source_rejects_non_positive_fps(tmp_path: Path) -> None:
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    cv2.imwrite(str(frames_dir / "000.jpg"), _make_frame(0))
    with pytest.raises(ValueError):
        FolderFramesSource(frames_dir, fps=0.0)


def test_folder_source_sorts_by_filename(tmp_path: Path) -> None:
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    # Write out of order on disk; iteration must sort them.
    for name, fill in [("002.jpg", 40), ("000.jpg", 10), ("001.jpg", 20)]:
        cv2.imwrite(str(frames_dir / name), _make_frame(fill))

    src = FolderFramesSource(frames_dir, fps=10.0)
    items = list(src.frames())

    assert src.fps == pytest.approx(10.0)
    fills = [int(frame[0, 0, 0]) for frame, _, _ in items]
    assert fills == [10, 20, 40]
    assert [fid for _, _, fid in items] == [0, 1, 2]
    ts = [t for _, t, _ in items]
    assert ts == pytest.approx([0.0, 0.1, 0.2])


def test_folder_source_skips_undecodable_files(tmp_path: Path) -> None:
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    cv2.imwrite(str(frames_dir / "000.jpg"), _make_frame(10))
    (frames_dir / "001.jpg").write_bytes(b"not an image")
    cv2.imwrite(str(frames_dir / "002.jpg"), _make_frame(30))

    src = FolderFramesSource(frames_dir, fps=5.0)
    items = list(src.frames())

    assert len(items) == 2
    assert int(items[0][0][0, 0, 0]) == 10
    assert int(items[1][0][0, 0, 0]) == 30


# ── RTSPVideoSource ─────────────────────────────────────────────────


class _FakeCapture:
    """Minimal ``cv2.VideoCapture`` stand-in driven by a script of events."""

    def __init__(self, script: list[str | np.ndarray]) -> None:
        self._script = list(script)
        self._opened = True

    def is_opened(self) -> bool:
        return self._opened

    def read(self) -> tuple[bool, np.ndarray | None]:
        if not self._script:
            return False, None
        item = self._script.pop(0)
        if isinstance(item, str) and item == "fail":
            return False, None
        assert isinstance(item, np.ndarray)
        return True, item

    def release(self) -> None:
        self._opened = False


def test_rtsp_rejects_empty_url() -> None:
    with pytest.raises(ValueError):
        RTSPVideoSource("")


def test_rtsp_exposes_fps_hint() -> None:
    src = RTSPVideoSource("rtsp://fake/stream", fps_hint=12.5)
    assert src.fps == pytest.approx(12.5)


def test_rtsp_reconnects_on_transient_read_failure() -> None:
    f0 = _make_frame(10)
    f1 = _make_frame(20)
    captures: Iterator[_FakeCapture] = iter(
        [
            _FakeCapture([f0, "fail"]),  # 1 frame, then failure → reconnect
            _FakeCapture([f1]),  # 1 frame, then EOS → second reconnect
        ]
    )
    sleeps: list[float] = []

    src = RTSPVideoSource(
        "rtsp://fake/stream",
        reconnect_initial_sec=0.01,
        reconnect_max_sec=0.04,
        max_reconnect_attempts=3,
        sleep_fn=sleeps.append,
        capture_factory=lambda _url: next(captures),
    )

    with pytest.raises(RuntimeError):
        list(src.frames())

    # Two frames yielded before giving up: one per successful open.
    assert sleeps, "expected at least one reconnect sleep"
    assert all(s <= 0.04 for s in sleeps)


def test_rtsp_close_stops_iteration() -> None:
    frames = [_make_frame(i) for i in range(3)]
    cap = _FakeCapture(list(frames) + ["fail"] * 10)
    src = RTSPVideoSource(
        "rtsp://fake/stream",
        reconnect_initial_sec=0.0,
        reconnect_max_sec=0.0,
        max_reconnect_attempts=5,
        sleep_fn=lambda _s: None,
        capture_factory=lambda _url: cap,
    )

    collected: list[int] = []
    it = src.frames()
    for _, _, fid in it:
        collected.append(fid)
        if len(collected) == 2:
            src.close()

    assert collected == [0, 1]
    assert not cap.is_opened()


# ── MjpegHTTPSource ─────────────────────────────────────────────────


def _encode_jpeg(fill: int) -> bytes:
    frame = _make_frame(fill)
    ok, buf = cv2.imencode(".jpg", frame)
    assert ok
    return buf.tobytes()


def _mjpeg_multipart(jpegs: list[bytes], boundary: bytes = b"--frame") -> bytes:
    """Glue JPEG payloads with MIME multipart framing (parser only scans SOI/EOI)."""
    parts: list[bytes] = []
    for jpg in jpegs:
        parts.append(boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n")
        parts.append(jpg)
        parts.append(b"\r\n")
    parts.append(boundary + b"--\r\n")
    return b"".join(parts)


class _FakeHttpStream:
    """File-like object returning a fixed byte sequence in slices."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0
        self.closed = False

    def read(self, amt: int | None = None) -> bytes:
        if self.closed or self._offset >= len(self._payload):
            return b""
        n = len(self._payload) - self._offset if amt is None else int(amt)
        stop = self._offset + n
        chunk = self._payload[slice(self._offset, stop)]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


def test_mjpeg_rejects_empty_url() -> None:
    with pytest.raises(ValueError):
        MjpegHTTPSource("")


def test_mjpeg_decodes_multipart_stream() -> None:
    jpegs = [_encode_jpeg(i * 30) for i in range(3)]
    stream = _FakeHttpStream(_mjpeg_multipart(jpegs))

    src = MjpegHTTPSource(
        "http://fake/stream.mjpg",
        max_reconnect_attempts=1,
        chunk_size=4096,
        sleep_fn=lambda _s: None,
        http_opener=lambda _u, _c, _r: stream,
    )

    frames: list[tuple[np.ndarray, float, int]] = []
    with pytest.raises(RuntimeError):  # end-of-stream → reconnect budget exhausted
        for item in src.frames():
            frames.append(item)

    assert [fid for _, _, fid in frames] == [0, 1, 2]
    for frame, _ts, _fid in frames:
        assert frame.shape == (32, 48, 3)
    assert stream.closed is True


def test_mjpeg_close_stops_iteration() -> None:
    jpegs = [_encode_jpeg(i * 30) for i in range(5)]
    stream = _FakeHttpStream(_mjpeg_multipart(jpegs))

    src = MjpegHTTPSource(
        "http://fake/stream.mjpg",
        max_reconnect_attempts=5,
        chunk_size=4096,
        sleep_fn=lambda _s: None,
        http_opener=lambda _u, _c, _r: stream,
    )

    collected: list[int] = []
    for _frame, _ts, fid in src.frames():
        collected.append(fid)
        if len(collected) == 2:
            src.close()
            break

    assert collected == [0, 1]
    assert stream.closed is True


def test_mjpeg_reconnects_on_connect_failure() -> None:
    opens: list[int] = []
    sleeps: list[float] = []

    def _always_fail(_url: str, _c: float, _r: float) -> _FakeHttpStream:
        opens.append(1)
        raise OSError("simulated connect failure")

    src = MjpegHTTPSource(
        "http://fake/stream.mjpg",
        reconnect_initial_sec=0.01,
        reconnect_max_sec=0.02,
        max_reconnect_attempts=3,
        sleep_fn=sleeps.append,
        http_opener=_always_fail,
    )

    with pytest.raises(RuntimeError, match="MJPEG connect failed"):
        list(src.frames())

    assert len(opens) == 3
    assert len(sleeps) == 2  # sleep between attempts, not after the last
