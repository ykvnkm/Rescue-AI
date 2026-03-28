"""Core domain entities."""

from __future__ import annotations

from dataclasses import dataclass, field

from rescue_ai.domain.value_objects import AlertStatus


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
    slug: str | None = None


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
    label: str
    model_name: str
    explanation: str | None = None


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
    status: AlertStatus = AlertStatus.QUEUED
    reviewed_by: str | None = None
    reviewed_at_sec: float | None = None
    decision_reason: str | None = None
