"""Unit tests for :class:`FFmpegRTSPSource` using a fake subprocess."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from typing import cast

import cv2
import numpy as np
import pytest

from rescue_ai.infrastructure.video.ffmpeg_rtsp_source import FFmpegRTSPSource


def _encode_jpeg(fill: int) -> bytes:
    frame = np.full((32, 48, 3), fill % 256, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", frame)
    assert ok
    return buf.tobytes()


class _PipePair:
    """Stand-in for a ``Popen.stdout`` / ``stderr`` with a ``fileno()``."""

    def __init__(self) -> None:
        self._r, self._w = os.pipe()

    def fileno(self) -> int:
        return self._r

    def write_bytes(self, data: bytes) -> None:
        if data:
            os.write(self._w, data)

    def close(self) -> None:
        for fd in (self._r, self._w):
            try:
                os.close(fd)
            except OSError:
                pass


class _FakeProc:
    """Minimal substitute for ``subprocess.Popen[bytes]``."""

    def __init__(self, stdout_payload: bytes, stderr_payload: bytes = b"") -> None:
        self.stdout = _PipePair()
        self.stderr = _PipePair()
        self.stdout.write_bytes(stdout_payload)
        self.stderr.write_bytes(stderr_payload)
        self._alive = True
        self._exit_code: int | None = None

    def poll(self) -> int | None:
        return self._exit_code

    def terminate(self) -> None:
        self._alive = False
        self._exit_code = 0
        self.stdout.close()
        self.stderr.close()

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout: float | None = None) -> int:
        _ = timeout
        self.terminate()
        return 0


def test_requires_ffmpeg_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "rescue_ai.infrastructure.video.ffmpeg_rtsp_source.shutil.which",
        lambda _name: None,
    )
    with pytest.raises(RuntimeError, match="ffmpeg"):
        FFmpegRTSPSource("rtsp://fake/stream")


def test_rejects_empty_url() -> None:
    with pytest.raises(ValueError):
        FFmpegRTSPSource("", ffmpeg_binary="/usr/bin/true")


def test_decodes_mjpeg_from_fake_ffmpeg() -> None:
    payload = b"".join(_encode_jpeg(i * 40) for i in range(3))
    procs: list[_FakeProc] = [_FakeProc(payload)]

    def _spawn(_cmd: Sequence[str]) -> subprocess.Popen[bytes]:
        return cast(subprocess.Popen[bytes], procs.pop(0))

    src = FFmpegRTSPSource(
        "rtsp://fake/stream",
        ffmpeg_binary="/usr/bin/true",
        max_open_attempts=1,
        open_deadline_sec=1.0,
        read_deadline_sec=0.2,
        chunk_size=4096,
        sleep_fn=lambda _s: None,
        process_factory=_spawn,
    )

    frames = list(src.frames())
    assert [fid for _, _, fid in frames] == [0, 1, 2]
    for frame, _ts, _fid in frames:
        assert frame.shape == (32, 48, 3)


def test_open_failure_records_stderr_tail() -> None:
    # stdout empty → no JPEG found within deadline → open fails
    proc = _FakeProc(stdout_payload=b"", stderr_payload=b"[rtsp] 404 not found")
    procs = [proc]

    def _spawn(_cmd: Sequence[str]) -> subprocess.Popen[bytes]:
        return cast(subprocess.Popen[bytes], procs.pop(0))

    src = FFmpegRTSPSource(
        "rtsp://fake/stream",
        ffmpeg_binary="/usr/bin/true",
        max_open_attempts=1,
        open_deadline_sec=0.05,
        read_deadline_sec=0.05,
        sleep_fn=lambda _s: None,
        process_factory=_spawn,
    )

    with pytest.raises(RuntimeError, match="ffmpeg open failed"):
        list(src.frames())
    assert "404 not found" in src.last_error
