from dataclasses import dataclass
from typing import Optional


@dataclass
class Mission:
    """Mission state entity."""

    mission_id: str
    status: str


@dataclass
class Alert:
    """Alert entity produced by detection pipeline."""

    alert_id: str
    mission_id: str
    frame_id: int
    ts_sec: float
    score: float
    status: str
    reviewed_by: Optional[str] = None
