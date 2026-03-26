"""Frame source service for timestamp computation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TimestampInputs:
    """Inputs required for stable frame timestamp calculation."""

    idx: int
    frame_path: Path
    fps: float
    base_frame_num: int | None
    prev_ts_sec: float


class FrameSourceService:
    """Lists mission frames and computes stable timeline timestamps."""

    def list_frame_files(self, frames_path: Path) -> list[Path]:
        """Return sorted image files from the given frames directory."""
        return sorted(
            path
            for path in frames_path.iterdir()
            if path.suffix.lower() in {".png", ".jpg", ".jpeg"}
        )

    def compute_ts_sec(self, inputs: TimestampInputs) -> float:
        """Compute a stable timestamp in seconds for the given frame."""
        dt = 1.0 / inputs.fps if inputs.fps > 0 else 0.5
        if inputs.fps <= 0:
            return inputs.idx * dt

        frame_num = self.extract_frame_number(inputs.frame_path)
        if frame_num is None or inputs.base_frame_num is None:
            ts_sec = inputs.idx * dt
        else:
            ts_sec = max((frame_num - inputs.base_frame_num) / inputs.fps, 0.0)

        if ts_sec < inputs.prev_ts_sec:
            ts_sec = inputs.prev_ts_sec + dt
        return ts_sec

    def extract_frame_number(self, frame_path: Path) -> int | None:
        """Extract the numeric frame index from a frame filename."""
        stem = frame_path.stem
        parts = stem.split("_")
        if parts and parts[-1].isdigit():
            return int(parts[-1])
        match = re.search(r"(\d+)$", stem)
        if match is None:
            return None
        return int(match.group(1))
