from libs.core.application.contracts import (
    AlertRepository,
    FrameEventRepository,
    MissionRepository,
    ReviewDecision,
)
from libs.core.domain.entities import Alert, FrameEvent, Mission


class PostgresMissionRepository(MissionRepository):
    """Postgres implementation of mission repository."""

    def create(self, mission: Mission) -> None:
        raise NotImplementedError("Postgres integration not implemented yet")

    def get(self, mission_id: str) -> Mission | None:
        raise NotImplementedError("Postgres integration not implemented yet")

    def update_status(self, mission_id: str, status: str) -> Mission | None:
        raise NotImplementedError("Postgres integration not implemented yet")


class PostgresAlertRepository(AlertRepository):
    """Postgres implementation of alert repository."""

    def add(self, alert: Alert) -> None:
        raise NotImplementedError("Postgres integration not implemented yet")

    def get(self, alert_id: str) -> Alert | None:
        raise NotImplementedError("Postgres integration not implemented yet")

    def list(
        self,
        mission_id: str | None = None,
        status: str | None = None,
    ) -> list[Alert]:
        raise NotImplementedError("Postgres integration not implemented yet")

    def update_status(
        self,
        alert_id: str,
        decision: ReviewDecision,
    ) -> Alert | None:
        raise NotImplementedError("Postgres integration not implemented yet")


class PostgresFrameEventRepository(FrameEventRepository):
    """Postgres implementation of frame event repository."""

    def add(self, frame_event: FrameEvent) -> None:
        raise NotImplementedError("Postgres integration not implemented yet")

    def list_by_mission(self, mission_id: str) -> list[FrameEvent]:
        raise NotImplementedError("Postgres integration not implemented yet")
