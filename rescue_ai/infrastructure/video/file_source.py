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
    """Yield ``(frame_bgr, ts_sec, frame_id)`` tuples from a video file."""

    def __init__(self, path: str | Path, fps_override: float | None = None) -> None:
        self._path = str(path)
        if not Path(self._path).is_file():
            raise FileNotFoundError(f"video file not found: {self._path}")
        self._cap: cv2.VideoCapture | None = None
        self._fps_override = fps_override

    def frames(self) -> Iterator[tuple[np.ndarray, float, int]]:
        """Open the file, iterate decoded BGR frames, close on exhaustion."""
        cap = cv2.VideoCapture(self._path)
        if not cap.isOpened():
            raise RuntimeError(f"cannot open video file: {self._path}")
        self._cap = cap

        reported = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        fps = self._fps_override if self._fps_override is not None else reported
        if fps <= 0.0:
            fps = _DEFAULT_FPS
        dt = 1.0 / fps

        frame_id = 0
        try:
            while True:
                ok, frame = cap.read()
                if not ok or frame is None:
                    return
                yield frame, frame_id * dt, frame_id
                frame_id += 1
        finally:
            self.close()

    def close(self) -> None:
        """Release the underlying ``VideoCapture`` if open."""
        if self._cap is not None:
            self._cap.release()
            self._cap = None
