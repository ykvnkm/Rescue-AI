"""Directory-of-frames video source — mimics a camera from saved images.

Reads ``*.jpg``/``*.jpeg``/``*.png`` files sorted by filename and yields
them as BGR frames with synthetic timestamps derived from ``fps``.
Useful for replaying captured missions frame-by-frame without video
container overhead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import cv2
import numpy as np

_SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png"}


class FolderFramesSource:
    """Yield ``(frame_bgr, ts_sec, frame_id)`` tuples from a frames directory."""

    def __init__(self, directory: str | Path, fps: float = 30.0) -> None:
        if fps <= 0.0:
            raise ValueError(f"fps must be positive, got {fps}")
        self._dir = Path(directory)
        if not self._dir.is_dir():
            raise FileNotFoundError(f"frames directory not found: {self._dir}")
        self._fps = float(fps)
        self._files = sorted(
            p for p in self._dir.iterdir() if p.suffix.lower() in _SUPPORTED_SUFFIXES
        )
        if not self._files:
            raise ValueError(f"no frame files in directory: {self._dir}")

    @property
    def fps(self) -> float:
        """Synthetic FPS used for frame timestamps."""
        return self._fps

    def frames(self) -> Iterator[tuple[np.ndarray, float, int]]:
        """Iterate the sorted frame list; skip files that fail to decode."""
        dt = 1.0 / self._fps
        for frame_id, path in enumerate(self._files):
            frame = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if frame is None:
                continue
            yield frame, frame_id * dt, frame_id

    def close(self) -> None:
        """No-op — ``imread`` owns no persistent resources."""
