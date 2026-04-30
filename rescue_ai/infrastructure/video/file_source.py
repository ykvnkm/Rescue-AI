"""Local-file video source — thin wrapper over ``cv2.VideoCapture``.

Used for offline automatic-mode runs (pre-recorded flights, regression
tests). Timestamps are derived from the reported FPS of the container;
``fps_override`` lets callers pin a value when the metadata is wrong or
missing. Frame ids start at 0 and advance monotonically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import cv2
import numpy as np

_DEFAULT_FPS = 30.0


class FileVideoSource:
    """Yield ``(frame_bgr, ts_sec, frame_id)`` tuples from a video file.

    When ``loop=True`` the capture is re-opened on EOF so the iterator
    never exhausts — used by the UI's "Демо-цикл" toggle. Frame ids and
    timestamps continue monotonically across loops.
    """

    def __init__(
        self,
        path: str | Path,
        fps_override: float | None = None,
        *,
        loop: bool = False,
    ) -> None:
        self._path = str(path)
        if not Path(self._path).is_file():
            raise FileNotFoundError(f"video file not found: {self._path}")
        self._cap: cv2.VideoCapture | None = None
        self._fps_override = fps_override
        self._loop = bool(loop)
        self._closed = False

    def frames(self) -> Iterator[tuple[np.ndarray, float, int]]:
        """Open the file, iterate decoded BGR frames, close on exhaustion."""
        cap = cv2.VideoCapture(self._path)
        if not cap.isOpened():
            raise RuntimeError(f"cannot open video file: {self._path}")
        self._cap = cap

        dt = 1.0 / self._fps

        frame_id = 0
        try:
            while not self._closed:
                ok, frame = cap.read()
                if not ok or frame is None:
                    if not self._loop or self._closed:
                        return
                    cap.release()
                    cap = cv2.VideoCapture(self._path)
                    if not cap.isOpened():
                        raise RuntimeError(
                            f"cannot reopen video file for loop: {self._path}"
                        )
                    self._cap = cap
                    continue
                yield frame, frame_id * dt, frame_id
                frame_id += 1
        finally:
            self.close()

    def close(self) -> None:
        """Release the underlying ``VideoCapture`` if open."""
        self._closed = True
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _resolve_fps(self) -> float:
        if self._fps_override is not None and self._fps_override > 0.0:
            return float(self._fps_override)

        cap = cv2.VideoCapture(self._path)
        try:
            reported = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        finally:
            cap.release()
        return reported if reported > 0.0 else _DEFAULT_FPS
