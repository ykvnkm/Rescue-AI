"""HTTP MJPEG video source — ``multipart/x-mixed-replace`` boundary parser.

Some RPi streaming stacks expose the camera as an HTTP endpoint returning
``multipart/x-mixed-replace; boundary=frame`` with JPEG payloads, which
``cv2.VideoCapture`` does not decode reliably. This adapter fetches the
stream with a streaming HTTP GET and scans the byte buffer for
SOI/EOI markers (``\\xff\\xd8`` .. ``\\xff\\xd9``), decoding each full
JPEG with ``cv2.imdecode``.

Reconnect behaviour matches :class:`RTSPVideoSource`: on read/connect
failure the source sleeps with exponential backoff (clamped to
``reconnect_max_sec``) and retries up to ``max_reconnect_attempts``. The
internal buffer is capped at ``max_buffer_bytes`` to protect against
bloat from a corrupted stream.

Timestamps come from ``time.monotonic`` so they track wall-clock even
across reconnects.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterator, Protocol, cast

import cv2
import numpy as np

_DEFAULT_RECONNECT_INITIAL = 0.5
_DEFAULT_RECONNECT_MAX = 5.0
_DEFAULT_MAX_ATTEMPTS = 10
_DEFAULT_CHUNK_SIZE = 64 * 1024
_DEFAULT_MAX_BUFFER_BYTES = 8 * 1024 * 1024
_DEFAULT_CONNECT_TIMEOUT = 5.0
_DEFAULT_READ_TIMEOUT = 30.0

_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"


@dataclass(frozen=True)
class MjpegHTTPSettings:
    """Tuning knobs for :class:`MjpegHTTPSource` construction."""

    reconnect_initial_sec: float = _DEFAULT_RECONNECT_INITIAL
    reconnect_max_sec: float = _DEFAULT_RECONNECT_MAX
    max_reconnect_attempts: int = _DEFAULT_MAX_ATTEMPTS
    chunk_size: int = _DEFAULT_CHUNK_SIZE
    max_buffer_bytes: int = _DEFAULT_MAX_BUFFER_BYTES
    connect_timeout_sec: float = _DEFAULT_CONNECT_TIMEOUT
    read_timeout_sec: float = _DEFAULT_READ_TIMEOUT
    sleep_fn: Callable[[float], None] | None = None
    http_opener: HttpOpener | None = None


class HttpStreamLike(Protocol):
    """Minimal file-like contract for an open HTTP streaming response."""

    def read(self, amt: int | None = ...) -> bytes: ...

    def close(self) -> None: ...


HttpOpener = Callable[[str, float, float], HttpStreamLike]


def _default_opener(
    url: str, connect_timeout: float, read_timeout: float
) -> HttpStreamLike:
    """Open ``url`` with ``urllib.request`` honoring both timeouts.

    ``urllib`` exposes only a single ``timeout`` kwarg that applies to
    both connect and read. We use the larger of the two so slow streams
    are not cut off mid-frame.
    """
    _ = connect_timeout
    return urllib.request.urlopen(url, timeout=read_timeout)  # noqa: S310


class MjpegHTTPSource:
    """Yield ``(frame_bgr, ts_sec, frame_id)`` tuples from an HTTP MJPEG stream."""

    def __init__(
        self,
        url: str,
        *,
        settings: MjpegHTTPSettings | None = None,
        **legacy: object,
    ) -> None:
        if not url:
            raise ValueError("MJPEG url must be non-empty")
        cfg = settings or MjpegHTTPSettings()
        if legacy:
            reconnect_initial_sec = cast(
                float,
                legacy.get("reconnect_initial_sec", cfg.reconnect_initial_sec),
            )
            reconnect_max_sec = cast(
                float,
                legacy.get("reconnect_max_sec", cfg.reconnect_max_sec),
            )
            max_reconnect_attempts = cast(
                int,
                legacy.get("max_reconnect_attempts", cfg.max_reconnect_attempts),
            )
            chunk_size = cast(int, legacy.get("chunk_size", cfg.chunk_size))
            max_buffer_bytes = cast(
                int,
                legacy.get("max_buffer_bytes", cfg.max_buffer_bytes),
            )
            connect_timeout_sec = cast(
                float,
                legacy.get("connect_timeout_sec", cfg.connect_timeout_sec),
            )
            read_timeout_sec = cast(
                float,
                legacy.get("read_timeout_sec", cfg.read_timeout_sec),
            )
            sleep_fn = cast(
                Callable[[float], None] | None,
                legacy.get("sleep_fn", cfg.sleep_fn),
            )
            http_opener = cast(
                HttpOpener | None,
                legacy.get("http_opener", cfg.http_opener),
            )
            cfg = MjpegHTTPSettings(
                reconnect_initial_sec=reconnect_initial_sec,
                reconnect_max_sec=reconnect_max_sec,
                max_reconnect_attempts=max_reconnect_attempts,
                chunk_size=chunk_size,
                max_buffer_bytes=max_buffer_bytes,
                connect_timeout_sec=connect_timeout_sec,
                read_timeout_sec=read_timeout_sec,
                sleep_fn=sleep_fn,
                http_opener=http_opener,
            )
        self._url = url
        self._reconnect_initial = float(cfg.reconnect_initial_sec)
        self._reconnect_max = float(cfg.reconnect_max_sec)
        self._max_attempts = int(cfg.max_reconnect_attempts)
        self._chunk_size = max(1024, int(cfg.chunk_size))
        self._max_buffer_bytes = max(self._chunk_size * 2, int(cfg.max_buffer_bytes))
        self._connect_timeout = float(cfg.connect_timeout_sec)
        self._read_timeout = float(cfg.read_timeout_sec)
        self._sleep = cfg.sleep_fn or time.sleep
        self._opener = cfg.http_opener or _default_opener
        self._stream: HttpStreamLike | None = None
        self._buf = bytearray()
        self._closed = False

    def frames(self) -> Iterator[tuple[np.ndarray, float, int]]:
        """Read frames until closed; reconnect on transient failure."""
        frame_id = 0
        t0 = time.monotonic()
        attempts = 0
        backoff = self._reconnect_initial

        try:
            while not self._closed:
                if self._stream is None:
                    if not self._open():
                        attempts += 1
                        if attempts >= self._max_attempts:
                            raise RuntimeError(
                                f"MJPEG connect failed after {attempts} attempts: "
                                f"{self._url}"
                            )
                        self._sleep(backoff)
                        backoff = min(backoff * 2.0, self._reconnect_max)
                        continue
                    attempts = 0
                    backoff = self._reconnect_initial

                frame = self._decode_next_frame()
                if frame is None:
                    self._release()
                    attempts += 1
                    if attempts >= self._max_attempts:
                        raise RuntimeError(
                            f"MJPEG read failed after {attempts} attempts: "
                            f"{self._url}"
                        )
                    self._sleep(backoff)
                    backoff = min(backoff * 2.0, self._reconnect_max)
                    continue

                yield frame, time.monotonic() - t0, frame_id
                frame_id += 1
        finally:
            self.close()

    def close(self) -> None:
        """Mark closed and release the HTTP response — safe to call repeatedly."""
        self._closed = True
        self._release()
        self._buf.clear()

    def _open(self) -> bool:
        try:
            self._stream = self._opener(
                self._url, self._connect_timeout, self._read_timeout
            )
        except (OSError, urllib.error.URLError, ValueError):
            self._stream = None
            return False
        self._buf.clear()
        return True

    def _release(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except (OSError, RuntimeError):  # pragma: no cover
                pass
            self._stream = None

    def _decode_next_frame(self) -> np.ndarray | None:
        """Pull chunks until a full JPEG is framed; return the decoded image."""
        if self._stream is None:
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
                # Malformed JPEG — drop and keep scanning.
                continue

            try:
                chunk = self._stream.read(self._chunk_size)
            except (OSError, urllib.error.URLError, ValueError):
                return None
            if not chunk:
                return None
            self._buf.extend(chunk)
            if len(self._buf) > self._max_buffer_bytes:
                # Corrupted stream: keep a small tail and drop the rest so
                # the next SOI has a chance of being aligned.
                del self._buf[: -self._chunk_size]
        return None
