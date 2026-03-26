"""Core domain entities: Mission, FrameEvent, Detection, Alert, config VOs."""

from __future__ import annotations

from dataclasses import dataclass, field
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
    completed_frame_id: int | None = None


@dataclass
class FrameEvent:
    """Frame event stored for mission timeline and GT episodes."""

    mission_id: str
    frame_id: int
    ts_sec: float
    image_uri: str
    gt_person_present: bool
    gt_episode_id: str | None


@dataclass(frozen=True)
class Detection:
    """Unified detection output used by YOLO detector, alert policy, and API."""

    bbox: tuple[float, float, float, float]
    score: float
    label: str = "person"
    model_name: str = "yolo8n"
    explanation: Optional[str] = None


@dataclass
class Alert:
    """Alert produced by detection pipeline."""

    alert_id: str
    mission_id: str
    frame_id: int
    ts_sec: float
    image_uri: str
    people_detected: int
    primary_detection: Detection
    detections: list[Detection] = field(default_factory=list)
    status: str = "queued"
    reviewed_by: Optional[str] = None
    reviewed_at_sec: Optional[float] = None
    decision_reason: Optional[str] = None


@dataclass(frozen=True)
class AlertRuleConfig:
    """Alert sliding-window thresholds loaded from contract YAML."""

    score_threshold: float = 0.2
    window_sec: float = 1.0
    quorum_k: int = 1
    cooldown_sec: float = 1.5
    gap_end_sec: float = 1.2
    gt_gap_end_sec: float = 1.0
    match_tolerance_sec: float = 1.2


@dataclass(frozen=True)
class InferenceConfig:
    """YOLO inference settings loaded from runtime configuration."""

    model_url: str
    device: str
    imgsz: int
    nms_iou: float
    max_det: int
    confidence_threshold: float


@dataclass(frozen=True)
class ArtifactBlob:
    """Binary artifact payload returned by artifact storage adapters."""

    content: bytes
    media_type: str
    filename: str
