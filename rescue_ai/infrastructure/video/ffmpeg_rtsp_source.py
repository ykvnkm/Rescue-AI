"""FFmpeg subprocess fallback for RTSP streams.

``cv2.VideoCapture`` over RTSP stalls on high-jitter networks on Raspberry
Pi; diplom-prod solved this with an ``ffmpeg`` subprocess that transcodes
the stream to MJPEG on stdout and scans the byte stream for
SOI/EOI markers — the same parsing approach as
:class:`MjpegHTTPSource`.

Transport is probed in order (auto → tcp → udp) up to
``max_open_attempts`` times. The ffmpeg binary is located via
``shutil.which`` at construction time; if it is absent the source raises
immediately so callers can fall back to cv2 or surface the error.

Timestamps come from ``time.monotonic`` so they track wall-clock across
reconnects.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess  # noqa: S404 - ffmpeg subprocess is deliberate
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Callable, Iterator, cast

import cv2
import numpy as np

_DEFAULT_MAX_OPEN_ATTEMPTS = 5
_DEFAULT_OPEN_DEADLINE_SEC = 4.0
_DEFAULT_READ_DEADLINE_SEC = 2.0
_DEFAULT_CHUNK_SIZE = 64 * 1024
_DEFAULT_MAX_BUFFER_BYTES = 16 * 1024 * 1024
_DEFAULT_STDERR_TAIL_BYTES = 32 * 1024
_DEFAULT_TRANSPORTS: tuple[str | None, ...] = (None, "tcp", "udp")

_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"


@dataclass(frozen=True)
class FFmpegRTSPSettings:
    """Tuning knobs for :class:`FFmpegRTSPSource` construction."""

    ffmpeg_binary: str | None = None
    transports: Sequence[str | None] = _DEFAULT_TRANSPORTS
    max_open_attempts: int = _DEFAULT_MAX_OPEN_ATTEMPTS
    open_deadline_sec: float = _DEFAULT_OPEN_DEADLINE_SEC
    read_deadline_sec: float = _DEFAULT_READ_DEADLINE_SEC
    chunk_size: int = _DEFAULT_CHUNK_SIZE
    max_buffer_bytes: int = _DEFAULT_MAX_BUFFER_BYTES
    sleep_fn: Callable[[float], None] | None = None
    process_factory: ProcessFactory | None = None


class _ProcessLike:
    """Protocol: only the bits of ``subprocess.Popen`` we use."""

    stdout: object
    stderr: object

    def poll(self) -> int | None:  # pragma: no cover - interface only
        raise NotImplementedError

    def terminate(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def kill(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def wait(self, timeout: float | None = ...) -> int:  # pragma: no cover
        raise NotImplementedError


ProcessFactory = Callable[[Sequence[str]], subprocess.Popen[bytes]]


def _set_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def _default_process_factory(cmd: Sequence[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - args fully constructed internally
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )


class FFmpegRTSPSource:
    """Yield ``(frame_bgr, ts_sec, frame_id)`` tuples via an ``ffmpeg`` subprocess."""

    def __init__(
        self,
        url: str,
        *,
        settings: FFmpegRTSPSettings | None = None,
        **legacy: object,
    ) -> None:
        if not url:
            raise ValueError("RTSP url must be non-empty")
        cfg = settings or FFmpegRTSPSettings()
        if legacy:
            ffmpeg_binary = cast(
                str | None, legacy.get("ffmpeg_binary", cfg.ffmpeg_binary)
            )
            transports = cast(
                Sequence[str | None],
                legacy.get("transports", cfg.transports),
            )
            max_open_attempts = cast(
                int,
                legacy.get("max_open_attempts", cfg.max_open_attempts),
            )
            open_deadline_sec = cast(
                float,
                legacy.get("open_deadline_sec", cfg.open_deadline_sec),
            )
            read_deadline_sec = cast(
                float,
                legacy.get("read_deadline_sec", cfg.read_deadline_sec),
            )
            chunk_size = cast(int, legacy.get("chunk_size", cfg.chunk_size))
            max_buffer_bytes = cast(
                int,
                legacy.get("max_buffer_bytes", cfg.max_buffer_bytes),
            )
            sleep_fn = cast(
                Callable[[float], None] | None,
                legacy.get("sleep_fn", cfg.sleep_fn),
            )
            process_factory = cast(
                ProcessFactory | None,
                legacy.get("process_factory", cfg.process_factory),
            )
            cfg = FFmpegRTSPSettings(
                ffmpeg_binary=ffmpeg_binary,
                transports=transports,
                max_open_attempts=max_open_attempts,
                open_deadline_sec=open_deadline_sec,
                read_deadline_sec=read_deadline_sec,
                chunk_size=chunk_size,
                max_buffer_bytes=max_buffer_bytes,
                sleep_fn=sleep_fn,
                process_factory=process_factory,
            )

        binary = cfg.ffmpeg_binary or shutil.which("ffmpeg")
        if not binary:
            raise RuntimeError("ffmpeg binary not found on PATH")
        self._url = url
        self._binary = binary
        self._transports = tuple(cfg.transports) or _DEFAULT_TRANSPORTS
        self._max_open_attempts = int(cfg.max_open_attempts)
        self._open_deadline = float(cfg.open_deadline_sec)
        self._read_deadline = float(cfg.read_deadline_sec)
        self._chunk_size = max(1024, int(cfg.chunk_size))
        self._max_buffer_bytes = max(self._chunk_size * 2, int(cfg.max_buffer_bytes))
        self._sleep = cfg.sleep_fn or time.sleep
        self._spawn = cfg.process_factory or _default_process_factory

        self._proc: subprocess.Popen[bytes] | None = None
        self._stdout_fd: int | None = None
        self._stderr_fd: int | None = None
        self._buf = bytearray()
        self._prefetched: np.ndarray | None = None
        self._last_error: str = ""
        self._closed = False

    @property
    def last_error(self) -> str:
        return self._last_error

    def frames(self) -> Iterator[tuple[np.ndarray, float, int]]:
        """Open ffmpeg and stream decoded frames until closed or ffmpeg dies."""
        frame_id = 0
        t0 = time.monotonic()

        try:
            if not self._open():
                raise RuntimeError(
                    f"ffmpeg open failed ({self._url}): {self._last_error}"
                )

            if self._prefetched is not None:
                yield self._prefetched, time.monotonic() - t0, frame_id
                frame_id += 1
                self._prefetched = None

            while not self._closed:
                frame = self._decode_next_frame(
                    deadline=time.monotonic() + self._read_deadline
                )
                if frame is None:
                    break
                yield frame, time.monotonic() - t0, frame_id
                frame_id += 1
        finally:
            self.close()

    def close(self) -> None:
        """Stop ffmpeg and release descriptors — safe to call repeatedly."""
        self._closed = True
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:  # pragma: no cover
                    pass
        self._proc = None
        self._stdout_fd = None
        self._stderr_fd = None
        self._buf.clear()

    # ── Internal ──────────────────────────────────────────────────

    def _build_command(self, transport: str | None) -> list[str]:
        cmd = [self._binary, "-hide_banner", "-loglevel", "error"]
        if transport:
            cmd += ["-rtsp_transport", transport]
        cmd += ["-i", self._url, "-an", "-f", "mjpeg", "-q:v", "5", "pipe:1"]
        return cmd

    def _open(self) -> bool:
        for attempt in range(self._max_open_attempts):
            transport = self._transports[attempt % len(self._transports)]
            try:
                proc = self._spawn(self._build_command(transport))
            except (OSError, ValueError) as error:
                self._last_error = f"spawn failed: {error}"
                self._sleep(0.3)
                continue

            self._proc = proc
            stdout = getattr(proc, "stdout", None)
            stderr = getattr(proc, "stderr", None)
            if stdout is None:
                self._last_error = "ffmpeg stdout unavailable"
                self.close()
                continue
            try:
                stdout_fd = stdout.fileno()
                self._stdout_fd = stdout_fd
                _set_nonblocking(stdout_fd)
                if stderr is not None:
                    stderr_fd = stderr.fileno()
                    self._stderr_fd = stderr_fd
                    _set_nonblocking(stderr_fd)
            except (OSError, AttributeError) as error:
                self._last_error = f"fd setup failed: {error}"
                self.close()
                continue

            frame = self._decode_next_frame(
                deadline=time.monotonic() + self._open_deadline
            )
            if frame is not None:
                self._prefetched = frame
                return True

            tail = self._read_stderr_tail()
            self._last_error = (
                f"[transport={transport or 'auto'}] {tail}"
                if tail
                else self._last_error
            )
            self.close()
            self._closed = False  # allow the retry loop to keep trying
            self._sleep(0.35)
        return False

    def _decode_next_frame(self, *, deadline: float) -> np.ndarray | None:
        if self._proc is None or self._stdout_fd is None:
            return None
        while not self._closed:
            start = self._buf.find(_SOI)
            end = self._buf.find(_EOI, start + 2) if start >= 0 else -1
            if 0 <= start < end:
                stop = end + 2
                jpg = bytes(self._buf[start:stop])
                del self._buf[:stop]
                decoded = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                if decoded is not None:
                    return decoded
                continue

            if time.monotonic() > deadline:
                return None
            try:
                chunk = os.read(self._stdout_fd, self._chunk_size)
            except BlockingIOError:
                chunk = b""
            except OSError:
                return None
            if not chunk:
                if self._proc.poll() is not None:
                    return None
                self._sleep(0.01)
                continue
            self._buf.extend(chunk)
            if len(self._buf) > self._max_buffer_bytes:
                del self._buf[: -self._chunk_size]
        return None

    def _read_stderr_tail(self) -> str:
        if self._stderr_fd is None:
            return ""
        chunks: list[bytes] = []
        while True:
            try:
                part = os.read(self._stderr_fd, _DEFAULT_STDERR_TAIL_BYTES)
            except BlockingIOError:
                break
            except OSError:
                break
            if not part:
                break
            chunks.append(part)
        raw = b"".join(chunks)
        if not raw:
            return ""
        try:
            return raw.decode("utf-8", errors="ignore").strip()[-500:]
        except (UnicodeDecodeError, ValueError):  # pragma: no cover
            return ""
