from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AlertRuleConfig:
    """Alert contract used for online ingestion and mission report."""

    score_threshold: float = 0.2
    window_sec: float = 1.0
    quorum_k: int = 1
    cooldown_sec: float = 1.5
    gap_end_sec: float = 1.2
    gt_gap_end_sec: float = 1.0
    match_tolerance_sec: float = 1.2


@dataclass
class DetectionInput:
    """Detection input received from frame ingestion endpoint."""

    bbox: tuple[float, float, float, float]
    score: float
    label: str = "person"
    model_name: str = "yolo8n"
    explanation: str | None = None
