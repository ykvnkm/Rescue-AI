"""In-memory repositories used by API gateway."""

from dataclasses import dataclass, field

from libs.core.application.contracts import (
    AlertRepository,
    FrameEventRepository,
    MissionRepository,
    ReviewDecision,
)
from libs.core.domain.entities import Alert, FrameEvent, Mission


@dataclass
class InMemoryDatabase:
    """Simple in-memory state container."""

    missions: dict[str, Mission] = field(default_factory=dict)
    alerts: dict[str, Alert] = field(default_factory=dict)
    mission_frames: dict[str, list[FrameEvent]] = field(default_factory=dict)


class InMemoryMissionRepository(MissionRepository):
    """Mission repository backed by in-memory dictionary."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def create(self, mission: Mission) -> None:
        self._db.missions[mission.mission_id] = mission
        self._db.mission_frames.setdefault(mission.mission_id, [])

    def get(self, mission_id: str) -> Mission | None:
        return self._db.missions.get(mission_id)

    def update_status(self, mission_id: str, status: str) -> Mission | None:
        mission = self._db.missions.get(mission_id)
        if mission is None:
            return None
        mission.status = status
        return mission


class InMemoryAlertRepository(AlertRepository):
    """Alert repository backed by in-memory dictionary."""

    allowed_target_statuses = {"reviewed_confirmed", "reviewed_rejected"}

    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def add(self, alert: Alert) -> None:
        self._db.alerts[alert.alert_id] = alert

    def get(self, alert_id: str) -> Alert | None:
        return self._db.alerts.get(alert_id)

    def list(
        self,
        mission_id: str | None = None,
        status: str | None = None,
    ) -> list[Alert]:
        alerts = list(self._db.alerts.values())
        if mission_id is not None:
            alerts = [alert for alert in alerts if alert.mission_id == mission_id]
        if status is not None:
            alerts = [alert for alert in alerts if alert.lifecycle.status == status]
        return sorted(alerts, key=lambda alert: (alert.ts_sec, alert.frame_id))

    def update_status(
        self,
        alert_id: str,
        decision: ReviewDecision,
    ) -> Alert | None:
        alert = self._db.alerts.get(alert_id)
        if alert is None:
            return None
        if decision["status"] not in self.allowed_target_statuses:
            raise ValueError("Invalid target status")
        if alert.lifecycle.status != "queued":
            raise ValueError("Alert already reviewed")

        alert.lifecycle.status = decision["status"]
        alert.lifecycle.reviewed_by = decision["reviewed_by"]
        alert.lifecycle.reviewed_at_sec = (
            decision["reviewed_at_sec"]
            if decision["reviewed_at_sec"] is not None
            else alert.ts_sec
        )
        alert.lifecycle.decision_reason = decision["decision_reason"]
        return alert


class InMemoryFrameEventRepository(FrameEventRepository):
    """Frame event repository backed by in-memory dictionary."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def add(self, frame_event: FrameEvent) -> None:
        self._db.mission_frames.setdefault(frame_event.mission_id, []).append(
            frame_event
        )

    def list_by_mission(self, mission_id: str) -> list[FrameEvent]:
        return list(self._db.mission_frames.get(mission_id, []))
