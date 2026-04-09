"""DTOs shared between the batch pipeline and its S3 mission source."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FrameRecord:
    """Single frame metadata used by batch processing."""

    frame_id: int
    ts_sec: float
    frame_path: Path
    image_uri: str
    gt_person_present: bool
    is_corrupted: bool = False


@dataclass(frozen=True)
class MissionInput:
    """Resolved mission source for a concrete processing date."""

    source_uri: str
    frames: list[FrameRecord]
    gt_available: bool
