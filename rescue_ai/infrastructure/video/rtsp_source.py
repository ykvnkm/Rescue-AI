"""RTSP video source with reconnect — live-stream adapter for automatic mode.

Wraps ``cv2.VideoCapture`` over an RTSP URL. On read failure the loop
releases the capture, sleeps (exponential backoff clamped to
``reconnect_max_sec``), and reopens — up to ``max_reconnect_attempts``
consecutive failures before giving up. Timestamps come from
``time.monotonic`` so they track wall-clock even across reconnects.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Iterator, Protocol

import cv2
import numpy as np

_DEFAULT_RECONNECT_INITIAL = 0.5
_DEFAULT_RECONNECT_MAX = 5.0
_DEFAULT_MAX_ATTEMPTS = 10
# A freshly-opened RTSP capture often returns empty reads for a moment while
# the demuxer fills its buffer (esp. TCP-interleaved over a slow link). Those
# transient empties must NOT trigger a reconnect — tearing down a healthy
# capture and re-doing DESCRIBE/SETUP/PLAY (seconds each) loses the whole
# stream. We tolerate up to ``max_empty_reads`` consecutive empty reads
# (sleeping ``empty_read_sleep_sec`` between them) before treating the stream
# as broken and reconnecting. Default ≈ a few seconds of grace at the sleep
# cadence below — enough for warm-up without masking a real disconnect.
_DEFAULT_MAX_EMPTY_READS = 300
_DEFAULT_EMPTY_READ_SLEEP = 0.02

# Force RTSP media over TCP (interleaved) instead of FFmpeg's default UDP.
# Behind k8s CNI NAT the UDP RTP packets never route back to the pod, so the
# TCP control channel (DESCRIBE/SETUP on :8554) succeeds but no frames arrive
# and the read times out. TCP-interleaved RTP rides the control connection and
# traverses NAT. OpenCV reads this env only when a VideoCapture is constructed,
# so it is set narrowly around the open and any operator value is preserved.
_RTSP_TCP_CAPTURE_OPTS = "rtsp_transport;tcp"


class CaptureLike(Protocol):
    """Minimal capture contract shared by cv2 and tests."""

    def read(self) -> tuple[bool, np.ndarray | None]: ...

    def release(self) -> None: ...


def _is_capture_opened(cap: object) -> bool:
    """Check opened state for both cv2-style and test doubles."""
    cv2_style = getattr(cap, "isOpened", None)
    if callable(cv2_style):
        return bool(cv2_style())
    snake_style = getattr(cap, "is_opened", None)
    if callable(snake_style):
        return bool(snake_style())
    return False


class RTSPVideoSource:
    """Yield ``(frame_bgr, ts_sec, frame_id)`` tuples from an RTSP URL."""

    def __init__(
        self,
        url: str,
        *,
        reconnect_initial_sec: float = _DEFAULT_RECONNECT_INITIAL,
        reconnect_max_sec: float = _DEFAULT_RECONNECT_MAX,
        max_reconnect_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        fps_hint: float = 30.0,
        max_empty_reads: int = _DEFAULT_MAX_EMPTY_READS,
        empty_read_sleep_sec: float = _DEFAULT_EMPTY_READ_SLEEP,
        sleep_fn: Callable[[float], None] | None = None,
        capture_factory: Callable[[str], CaptureLike] | None = None,
    ) -> None:
        if not url:
            raise ValueError("RTSP url must be non-empty")
        self._url = url
        self._reconnect_initial = float(reconnect_initial_sec)
        self._reconnect_max = float(reconnect_max_sec)
        self._max_attempts = int(max_reconnect_attempts)
        self._fps_hint = float(fps_hint) if fps_hint > 0.0 else 30.0
        self._max_empty_reads = int(max_empty_reads)
        self._empty_read_sleep = float(empty_read_sleep_sec)
        self._sleep = sleep_fn or time.sleep
        self._capture_factory = capture_factory or cv2.VideoCapture
        self._cap: CaptureLike | None = None
        self._closed = False

    @property
    def fps(self) -> float:
        """Best-known stream FPS; live RTSP timestamps still use wall time."""
        return self._fps_hint

    def frames(self) -> Iterator[tuple[np.ndarray, float, int]]:
        """Read frames until closed; reconnect on transient failure."""
        frame_id = 0
        t0 = time.monotonic()
        attempts = 0
        backoff = self._reconnect_initial
        empty_reads = 0

        try:
            while not self._closed:
                if self._cap is None or not _is_capture_opened(self._cap):
                    if not self._open():
                        attempts += 1
                        if attempts >= self._max_attempts:
                            raise RuntimeError(
                                f"RTSP connect failed after {attempts} attempts: "
                                f"{self._url}"
                            )
                        self._sleep(backoff)
                        backoff = min(backoff * 2.0, self._reconnect_max)
                        continue
                    attempts = 0
                    backoff = self._reconnect_initial
                    empty_reads = 0

                cap = self._cap
                assert cap is not None
                ok, frame = cap.read()
                if not ok or frame is None:
                    # Tolerate transient empty reads (buffer warm-up) on a live
                    # capture: poll without tearing it down. Only a sustained
                    # run of empties means the stream is actually gone — then
                    # reconnect (counting one attempt against the budget).
                    empty_reads += 1
                    if empty_reads <= self._max_empty_reads:
                        self._sleep(self._empty_read_sleep)
                        continue
                    self._release()
                    empty_reads = 0
                    attempts += 1
                    if attempts >= self._max_attempts:
                        raise RuntimeError(
                            f"RTSP read failed after {attempts} attempts: {self._url}"
                        )
                    self._sleep(backoff)
                    backoff = min(backoff * 2.0, self._reconnect_max)
                    continue

                attempts = 0
                empty_reads = 0
                yield frame, time.monotonic() - t0, frame_id
                frame_id += 1
        finally:
            self.close()

    def close(self) -> None:
        """Mark closed and release the capture — safe to call repeatedly."""
        self._closed = True
        self._release()

    def _open(self) -> bool:
        cap = self._build_capture()
        if not _is_capture_opened(cap):
            cap.release()
            return False
        self._cap = cap
        return True

    def _build_capture(self) -> CaptureLike:
        """Create the capture with RTSP-over-TCP forced (see module note)."""
        prev = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
        if prev is None:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = _RTSP_TCP_CAPTURE_OPTS
        try:
            return self._capture_factory(self._url)
        finally:
            if prev is None:
                os.environ.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)

    def _release(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
