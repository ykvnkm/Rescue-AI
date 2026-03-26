"""Domain value objects."""

from dataclasses import dataclass


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
