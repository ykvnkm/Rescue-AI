# pylint: disable=too-few-public-methods,missing-class-docstring,duplicate-code
from dataclasses import dataclass, field

from libs.core.application.contracts import (
    AlertRepository,
    FrameEventRepository,
    MissionRepository,
    ReviewDecision,
)
from libs.core.domain.entities import Alert, FrameEvent, Mission


@dataclass
class InMemoryBatchDb:
    missions: dict[str, Mission] = field(default_factory=dict)
    alerts: dict[str, Alert] = field(default_factory=dict)
    frames: dict[str, list[FrameEvent]] = field(default_factory=dict)


class InMemoryMissionRepo(MissionRepository):
    def __init__(self, db: InMemoryBatchDb) -> None:
        self._db = db

    def create(self, mission: Mission) -> None:
        self._db.missions[mission.mission_id] = mission
        self._db.frames.setdefault(mission.mission_id, [])

    def get(self, mission_id: str) -> Mission | None:
        return self._db.missions.get(mission_id)

    def update_status(
        self,
        mission_id: str,
        status: str,
        completed_frame_id: int | None = None,
    ) -> Mission | None:
        mission = self._db.missions.get(mission_id)
        if mission is None:
            return None
        mission.status = status
        if completed_frame_id is not None:
            mission.completed_frame_id = completed_frame_id
        return mission


class InMemoryAlertRepo(AlertRepository):
    def __init__(self, db: InMemoryBatchDb) -> None:
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
        items = list(self._db.alerts.values())
        if mission_id is not None:
            items = [item for item in items if item.mission_id == mission_id]
        if status is not None:
            items = [item for item in items if item.lifecycle.status == status]
        return sorted(items, key=lambda item: (item.ts_sec, item.frame_id))

    def update_status(
        self,
        alert_id: str,
        decision: ReviewDecision,
    ) -> Alert | None:
        alert = self._db.alerts.get(alert_id)
        if alert is None:
            return None
        alert.lifecycle.status = decision["status"]
        alert.lifecycle.reviewed_by = decision["reviewed_by"]
        alert.lifecycle.reviewed_at_sec = decision["reviewed_at_sec"]
        alert.lifecycle.decision_reason = decision["decision_reason"]
        return alert


class InMemoryFrameEventRepo(FrameEventRepository):
    def __init__(self, db: InMemoryBatchDb) -> None:
        self._db = db

    def add(self, frame_event: FrameEvent) -> None:
        self._db.frames.setdefault(frame_event.mission_id, []).append(frame_event)

    def list_by_mission(self, mission_id: str) -> list[FrameEvent]:
        return list(self._db.frames.get(mission_id, []))
