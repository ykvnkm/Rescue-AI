"""Domain-level port interfaces (protocols) for dependency inversion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from rescue_ai.domain.entities import Alert, Detection, FrameEvent, Mission


@dataclass(frozen=True)
class ArtifactBlob:
    """Binary artifact payload returned by artifact storage adapters."""

    content: bytes
    media_type: str
    filename: str


class AlertReviewPayload(TypedDict):
    """Typed payload for applying an operator review to an alert."""

    status: Literal["reviewed_confirmed", "reviewed_rejected"]
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
    code_version: str
    ds: str
    model_version: str
    run_key: str


class MissionRepository(Protocol):
    """Mission persistence contract."""

    def create(self, mission: Mission) -> None:
        """Persist a new mission."""

    def get(self, mission_id: str) -> Mission | None:
        """Retrieve a mission by its identifier."""

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
    """Storage contract for mission artifacts (frames, reports, batch outputs)."""

    def store_frame(self, mission_id: str, frame_id: int, source_uri: str) -> str: ...

    def load_frame(self, image_uri: str) -> ArtifactBlob | None: ...

    def save_mission_report(
        self, mission_id: str, report: dict[str, object]
    ) -> str: ...

    def load_mission_report(self, mission_id: str) -> dict[str, object] | None: ...


class DetectorPort(Protocol):
    """Port for ML detector used by both online and batch services."""

    def detect(self, image_uri: str) -> list[Detection]: ...
    def warmup(self) -> None: ...
    def runtime_name(self) -> str: ...


class FramePublisherPort(Protocol):
    """Port for publishing frame payload into mission API."""

    def publish(
        self, mission_id: str, api_base: str, payload: FramePublishPayload
    ) -> None: ...
    def endpoint(self, mission_id: str, api_base: str) -> str: ...
