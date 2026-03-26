"""Domain-level port interfaces (protocols) for dependency inversion."""

from __future__ import annotations

from typing import Protocol

from rescue_ai.domain.entities import (
    Alert,
    ArtifactBlob,
    Detection,
    FrameEvent,
    Mission,
)


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
        updates: dict[str, object],
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

    def write_report(self, run_key: str, payload: dict[str, object]) -> str: ...

    def write_debug_rows(self, run_key: str, rows: list[dict[str, object]]) -> str: ...


class DetectorPort(Protocol):
    """Port for ML detector used by both online and batch services."""

    def detect(self, image_uri: str) -> list[Detection]: ...
    def warmup(self) -> None: ...
    def runtime_name(self) -> str: ...


class FramePublisherPort(Protocol):
    """Port for publishing frame payload into mission API."""

    def publish(
        self, mission_id: str, api_base: str, payload: dict[str, object]
    ) -> None: ...
    def endpoint(self, mission_id: str, api_base: str) -> str: ...
