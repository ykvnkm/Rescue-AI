"""Core domain entities."""

from __future__ import annotations

from dataclasses import dataclass, field

from rescue_ai.domain.value_objects import (
    AlertStatus,
    AutoDecisionKind,
    MissionMode,
    TrajectorySource,
)


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
    mode: MissionMode = MissionMode.OPERATOR


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


@dataclass(frozen=True)
class TrajectoryPoint:
    """Single estimated 3D pose sample for an automatic mission.

    Coordinates are expressed in meters relative to the mission's origin
    (first observed marker or first processed frame). ``seq`` is a
    per-mission monotonically increasing counter; ``frame_id`` is ``None``
    when the point was interpolated rather than computed from a frame.
    """

    mission_id: str
    seq: int
    ts_sec: float
    x: float
    y: float
    z: float
    source: TrajectorySource
    frame_id: int | None = None


@dataclass(frozen=True)
class AutoDecision:
    """Immutable audit record produced by the automatic pipeline.

    Automatic missions do not receive human review, so every decision the
    engine takes about a frame (emit an alert, suppress one, etc.) is
    appended here with a free-form ``reason`` string. ``created_at`` is an
    ISO-formatted timestamp in UTC.
    """

    decision_id: str
    mission_id: str
    ts_sec: float
    kind: AutoDecisionKind
    reason: str
    created_at: str
    frame_id: int | None = None
