"""Domain value objects."""

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True)
class ArtifactBlob:
    """Binary artifact payload returned by artifact storage adapters."""

    content: bytes
    media_type: str
    filename: str


@dataclass(frozen=True)
class AlertRuleConfig:
    """Alert sliding-window thresholds loaded from contract YAML."""

    score_threshold: float
    window_sec: float
    quorum_k: int
    cooldown_sec: float
    gap_end_sec: float
    gt_gap_end_sec: float
    match_tolerance_sec: float


class AlertStatus(StrEnum):
    """Alert lifecycle status in pilot mission flow."""

    QUEUED = "queued"
    REVIEWED_CONFIRMED = "reviewed_confirmed"
    REVIEWED_REJECTED = "reviewed_rejected"
