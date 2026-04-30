"""RTSP video source with reconnect — live-stream adapter for automatic mode.

Wraps ``cv2.VideoCapture`` over an RTSP URL. On read failure the loop
releases the capture, sleeps (exponential backoff clamped to
``reconnect_max_sec``), and reopens — up to ``max_reconnect_attempts``
consecutive failures before giving up. Timestamps come from
``time.monotonic`` so they track wall-clock even across reconnects.
"""

from __future__ import annotations

import time
from typing import Callable, Iterator, Protocol

import cv2
import numpy as np

_DEFAULT_RECONNECT_INITIAL = 0.5
_DEFAULT_RECONNECT_MAX = 5.0
_DEFAULT_MAX_ATTEMPTS = 10


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

                cap = self._cap
                assert cap is not None
                ok, frame = cap.read()
                if not ok or frame is None:
                    self._release()
                    attempts += 1
                    if attempts >= self._max_attempts:
                        raise RuntimeError(
                            f"RTSP read failed after {attempts} attempts: {self._url}"
                        )
                    self._sleep(backoff)
                    backoff = min(backoff * 2.0, self._reconnect_max)
                    continue

                yield frame, time.monotonic() - t0, frame_id
                frame_id += 1
        finally:
            self.close()

    def close(self) -> None:
        """Mark closed and release the capture — safe to call repeatedly."""
        self._closed = True
        self._release()

    def _open(self) -> bool:
        cap = self._capture_factory(self._url)
        if not _is_capture_opened(cap):
            cap.release()
            return False
        self._cap = cap
        return True

    def _release(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
