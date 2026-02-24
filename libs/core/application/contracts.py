from typing import Protocol, TypedDict

from libs.core.domain.entities import Alert, FrameEvent, Mission


class ReviewDecision(TypedDict):
    """Review decision payload passed to repositories."""

    status: str
    reviewed_by: str | None
    reviewed_at_sec: float | None
    decision_reason: str | None


class MissionRepository(Protocol):
    """Mission persistence contract."""

    def create(self, mission: Mission) -> None: ...

    def get(self, mission_id: str) -> Mission | None: ...

    def update_status(self, mission_id: str, status: str) -> Mission | None: ...


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
        decision: ReviewDecision,
    ) -> Alert | None: ...


class FrameEventRepository(Protocol):
    """Mission frame stream persistence contract."""

    def add(self, frame_event: FrameEvent) -> None: ...
    def list_by_mission(self, mission_id: str) -> list[FrameEvent]: ...
