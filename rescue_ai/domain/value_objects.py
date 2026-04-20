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


class MissionMode(StrEnum):
    """Mission operating mode discriminator.

    Operator missions route alerts through human review; automatic missions
    rely on the navigation engine and write decisions to an audit log without
    human involvement.
    """

    OPERATOR = "operator"
    AUTOMATIC = "automatic"


class NavMode(StrEnum):
    """Navigation mode selected for an automatic mission.

    ``AUTO`` means the application probes the first seconds of footage and
    elects ``MARKER`` or ``NO_MARKER`` based on whether a fiducial is
    detected.
    """

    MARKER = "marker"
    NO_MARKER = "no_marker"
    AUTO = "auto"


class TrajectorySource(StrEnum):
    """Which navigation subsystem emitted a trajectory point."""

    MARKER = "marker"
    OPTICAL_FLOW = "optical_flow"
    FALLBACK = "fallback"


class AutoDecisionKind(StrEnum):
    """Kind of automatic decision recorded for an auto mission.

    Serves as the automatic counterpart to operator review: instead of
    ``reviewed_by``/``decision_reason`` fields on the alert, the automatic
    pipeline appends an immutable decision record describing why an alert
    was or was not emitted for a frame.
    """

    ALERT_CREATED = "alert_created"
    ALERT_SUPPRESSED = "alert_suppressed"
