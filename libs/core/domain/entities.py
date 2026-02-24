from dataclasses import dataclass
from typing import Optional


@dataclass
class Mission:
    """Mission state entity."""

    mission_id: str
    source_name: str
    status: str
    created_at: str
    total_frames: int
    fps: float


@dataclass
class FrameEvent:
    """Frame event stored for mission timeline and GT episodes."""

    mission_id: str
    frame_id: int
    ts_sec: float
    image_uri: str
    gt_person_present: bool
    gt_episode_id: str | None


@dataclass
class DetectionData:
    """Detection payload attached to an alert."""

    bbox: tuple[float, float, float, float]
    score: float
    label: str
    model_name: str
    explanation: Optional[str] = None


@dataclass
class AlertLifecycle:
    """Lifecycle data for alert review state."""

    status: str
    reviewed_by: Optional[str] = None
    reviewed_at_sec: Optional[float] = None
    decision_reason: Optional[str] = None


@dataclass
class Alert:
    """Alert entity produced by detection pipeline."""

    alert_id: str
    mission_id: str
    frame_id: int
    ts_sec: float
    image_uri: str
    detection: DetectionData
    lifecycle: AlertLifecycle
