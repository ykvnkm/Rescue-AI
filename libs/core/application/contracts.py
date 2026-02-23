from typing import Protocol

from libs.core.domain.entities import Alert, Mission


class MissionRepository(Protocol):
    """Mission persistence contract."""

    def create(self, mission: Mission) -> None: ...
    def get(self, mission_id: str) -> Mission | None: ...


class AlertRepository(Protocol):
    """Alert persistence contract."""

    def add(self, alert: Alert) -> None: ...
    def get(self, alert_id: str) -> Alert | None: ...

    def update_status(
        self,
        alert_id: str,
        status: str,
        reviewed_by: str | None = None,
    ) -> None: ...
