from libs.core.application.contracts import AlertRepository, MissionRepository
from libs.core.domain.entities import Alert, Mission


class PostgresMissionRepository(MissionRepository):
    """Postgres implementation of mission repository."""

    def create(self, mission: Mission) -> None:
        raise NotImplementedError("Postgres integration not implemented yet")

    def get(self, mission_id: str) -> Mission | None:
        raise NotImplementedError("Postgres integration not implemented yet")


class PostgresAlertRepository(AlertRepository):
    """Postgres implementation of alert repository."""

    def add(self, alert: Alert) -> None:
        raise NotImplementedError("Postgres integration not implemented yet")

    def get(self, alert_id: str) -> Alert | None:
        raise NotImplementedError("Postgres integration not implemented yet")

    def update_status(
        self,
        alert_id: str,
        status: str,
        reviewed_by: str | None = None,
    ) -> None:
        raise NotImplementedError("Postgres integration not implemented yet")
