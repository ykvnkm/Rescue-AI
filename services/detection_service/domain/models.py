from __future__ import annotations

from dataclasses import dataclass

DEFAULT_ALERT_RULES: dict[str, float | int] = {
    "score_threshold": 0.2,
    "window_sec": 1.0,
    "quorum_k": 1,
    "cooldown_sec": 1.5,
    "gap_end_sec": 1.2,
    "gt_gap_end_sec": 1.0,
    "match_tolerance_sec": 1.2,
}


@dataclass(frozen=True)
class DetectionResult:
    """Single detector output after filtering and conversion."""

    bbox: tuple[float, float, float, float]
    score: float
    label: str = "person"


@dataclass(frozen=True)
class InferenceConfig:
    """Inference settings loaded from runtime configuration."""

    model_url: str
    device: str
    imgsz: int
    nms_iou: float
    max_det: int
    confidence_threshold: float


@dataclass(frozen=True)
class AlertRulesConfig:
    """Alert rules loaded from runtime configuration."""

    score_threshold: float = float(DEFAULT_ALERT_RULES["score_threshold"])
    window_sec: float = float(DEFAULT_ALERT_RULES["window_sec"])
    quorum_k: int = int(DEFAULT_ALERT_RULES["quorum_k"])
    cooldown_sec: float = float(DEFAULT_ALERT_RULES["cooldown_sec"])
    gap_end_sec: float = float(DEFAULT_ALERT_RULES["gap_end_sec"])
    gt_gap_end_sec: float = float(DEFAULT_ALERT_RULES["gt_gap_end_sec"])
    match_tolerance_sec: float = float(DEFAULT_ALERT_RULES["match_tolerance_sec"])


@dataclass(frozen=True)
class ReportProvenance:
    """Provenance metadata for generated reports."""

    config_name: str
    config_hash: str
    config_path: str
    service_version: str


@dataclass(frozen=True)
class StreamContract:
    """Full stream contract used by detection runtime."""

    dataset_fps: float
    alert_rules: AlertRulesConfig
    inference: InferenceConfig
    min_detections_per_frame: int
    report_provenance: ReportProvenance
