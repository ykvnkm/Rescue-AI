"""Domain-level port interfaces (protocols) for dependency inversion."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, TypedDict

from rescue_ai.domain.entities import (
    Alert,
    AutoDecision,
    Detection,
    FrameEvent,
    Mission,
    TrajectoryPoint,
)
from rescue_ai.domain.value_objects import AlertStatus, ArtifactBlob, NavMode


class AlertReviewPayload(TypedDict):
    """Typed payload for applying an operator review to an alert."""

    status: AlertStatus
    reviewed_by: str | None
    reviewed_at_sec: float | None
    decision_reason: str | None


class DetectionPayload(TypedDict):
    """Serialized detection item passed over frame-ingest API boundary."""

    bbox: list[float]
    score: float
    label: str
    model_name: str
    explanation: str | None


class FramePublishPayload(TypedDict):
    """Typed payload for publishing one frame event to the mission API."""

    frame_id: int
    ts_sec: float
    image_uri: str
    gt_person_present: bool
    gt_episode_id: str | None
    detections: list[DetectionPayload]


class ReportMetadataPayload(TypedDict, total=False):
    """Typed report metadata attached to mission reports."""

    config_name: str
    config_hash: str
    config_path: str
    model_url: str
    model_sha256: str
    service_version: str
    ds: str
    run_key: str


class MissionRepository(Protocol):
    """Mission persistence contract."""

    def create(self, mission: Mission) -> None:
        """Persist a new mission."""

    def get(self, mission_id: str) -> Mission | None:
        """Retrieve a mission by its identifier."""

    def list(self, status: str | None = None) -> list[Mission]:
        """List missions, optionally filtered by status."""

    def update_details(
        self,
        mission_id: str,
        *,
        source_name: str | None = None,
        total_frames: int | None = None,
        fps: float | None = None,
    ) -> Mission | None:
        """Update mutable mission metadata fields."""

    def update_status(
        self,
        mission_id: str,
        status: str,
        completed_frame_id: int | None = None,
    ) -> Mission | None:
        """Transition mission to a new status."""


class AlertRepository(Protocol):
    """Alert persistence contract."""

    def add(self, alert: Alert) -> None: ...

    def get(self, alert_id: str) -> Alert | None: ...

    def list(
        self,
        mission_id: str | None = None,
        status: str | None = None,
    ) -> list[Alert]: ...

    def update_status(
        self,
        alert_id: str,
        updates: AlertReviewPayload,
    ) -> Alert | None:
        """Apply a review decision to an alert."""


class FrameEventRepository(Protocol):
    """Mission frame stream persistence contract."""

    def add(self, frame_event: FrameEvent) -> None: ...
    def list_by_mission(self, mission_id: str) -> list[FrameEvent]: ...


class ArtifactStorage(Protocol):
    """Storage contract for mission artifacts (frames, reports, batch outputs).

    All mission-scoped writes are partitioned by ``ds`` (the date-string the
    mission belongs to, derived from its ``created_at``). The canonical layout
    is ``YYYY-MM-DD/{mission_id}/...`` so the offline batch DAG sees exactly
    the same partitioning the online side wrote.
    """

    def store_frame(
        self, mission_id: str, frame_id: int, source_uri: str, ds: str
    ) -> str: ...

    def load_frame(self, image_uri: str) -> ArtifactBlob | None: ...

    def save_mission_report(
        self, mission_id: str, ds: str, report: Mapping[str, object]
    ) -> str: ...

    def save_mission_annotations(
        self, mission_id: str, ds: str, payload: Mapping[str, object]
    ) -> str: ...

    def load_mission_report(
        self, mission_id: str, ds: str
    ) -> Mapping[str, object] | None: ...

    def save_trajectory_csv(
        self,
        mission_id: str,
        ds: str,
        points: Sequence[TrajectoryPoint],
    ) -> str:
        """Persist a mission trajectory as CSV; return the artifact URI.

        Used by automatic missions (ADR-0006): layout
        ``{prefix}/{ds}/{mission_id}/trajectory.csv``. Columns are
        ``seq,ts_sec,frame_id,x,y,z,source``.
        """

    def save_trajectory_plot(self, mission_id: str, ds: str, png_bytes: bytes) -> str:
        """Persist a rendered trajectory PNG under
        ``{prefix}/{ds}/{mission_id}/plots/trajectory.png``.

        Automatic missions (ADR-0006) use this alongside
        ``save_mission_report`` to keep the S3 layout uniform with
        operator missions (``report.json`` at the mission root).
        """


class DetectorPort(Protocol):
    """Port for ML detector used by both online and batch services."""

    def detect(self, image_uri: object) -> list[Detection]: ...
    def warmup(self) -> None: ...
    def runtime_name(self) -> str: ...


class FramePublisherPort(Protocol):
    """Port for publishing frame payload into mission API."""

    def publish(
        self, mission_id: str, api_base: str, payload: FramePublishPayload
    ) -> None: ...
    def endpoint(self, mission_id: str, api_base: str) -> str: ...


# ── Automatic-mode ports (consumed in P1.2+) ─────────────────────


class TrajectoryRepository(Protocol):
    """Persistence contract for automatic-mission trajectory points."""

    def add(self, point: TrajectoryPoint) -> None: ...

    def list_by_mission(self, mission_id: str) -> list[TrajectoryPoint]:
        """Return all points for a mission ordered by ``seq``."""


class AutoDecisionRepository(Protocol):
    """Append-only audit log for automatic-mode decisions."""

    def add(self, decision: AutoDecision) -> None: ...

    def list_by_mission(self, mission_id: str) -> list[AutoDecision]:
        """Return all decisions for a mission ordered by ``ts_sec``."""


class AutoMissionConfigRepository(Protocol):
    """Stores the per-mission automatic configuration snapshot.

    One row per mission captured when the mission starts, keeping the
    choice of ``nav_mode`` + ``detector`` + serialized navigation tuning
    reproducible for later analysis.
    """

    def save(
        self,
        *,
        mission_id: str,
        nav_mode: NavMode,
        detector: str,
        config_json: Mapping[str, object],
    ) -> None: ...

    def get(self, mission_id: str) -> Mapping[str, object] | None: ...


class TrajectoryPlotRendererPort(Protocol):
    """Port for rendering a PNG plot from a list of trajectory points.

    Implementations live in infrastructure (matplotlib/headless Agg). The
    domain only needs a ``(points) -> png_bytes`` contract so
    :class:`AutoMissionService` stays I/O-free.
    """

    def render(
        self,
        mission_id: str,
        points: Sequence[TrajectoryPoint],
    ) -> bytes:
        """Return PNG-encoded bytes of the trajectory plot."""


class NavigationEnginePort(Protocol):
    """Port for the navigation engine used by automatic missions.

    A navigation engine is a stateful object consuming frames in order and
    producing estimated poses. Implementations live under
    ``rescue_ai/navigation/`` (see P1.2) and must be free of I/O — frames
    are supplied by the caller and poses are returned synchronously.
    """

    def reset(self) -> None:
        """Clear internal state and start a new trajectory."""

    def step(
        self,
        frame_bgr: object,
        ts_sec: float,
        frame_id: int | None = None,
    ) -> TrajectoryPoint | None:
        """Advance the engine with one frame; return the new point or ``None``.

        Returning ``None`` means the engine could not update the pose for
        this frame (e.g. marker lost, tracking quality below threshold).
        """


class VideoFramePort(Protocol):
    """Port for a video frame source (RTSP, file, folder of images).

    Implementations live under ``rescue_ai/infrastructure/video/`` (see
    P1.3). Consumers iterate over ``frames()`` and call ``close()`` when
    done. Each tuple is ``(frame_bgr, ts_sec, frame_id)`` where
    ``frame_bgr`` is a numpy ``ndarray`` (typed as ``object`` here to
    avoid pulling numpy into the domain layer).
    """

    def frames(self) -> object:
        """Yield ``(frame_bgr, ts_sec, frame_id)`` tuples in capture order."""

    def close(self) -> None: ...
