"""In-memory repository implementations used only in tests."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from rescue_ai.domain.entities import Alert, FrameEvent, Mission
from rescue_ai.domain.ports import AlertReviewPayload
from rescue_ai.domain.value_objects import AlertStatus, ArtifactBlob


@dataclass
class InMemoryDatabase:
    missions: dict[str, Mission] = field(default_factory=dict)
    alerts: dict[str, Alert] = field(default_factory=dict)
    mission_frames: dict[str, list[FrameEvent]] = field(default_factory=dict)


class InMemoryMissionRepository:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def create(self, mission: Mission) -> None:
        self._db.missions[mission.mission_id] = mission
        self._db.mission_frames.setdefault(mission.mission_id, [])

    def get(self, mission_id: str) -> Mission | None:
        return self._db.missions.get(mission_id)

    def list(self, status: str | None = None) -> list[Mission]:
        missions = list(self._db.missions.values())
        if status is not None:
            missions = [mission for mission in missions if mission.status == status]
        return sorted(missions, key=lambda item: item.created_at)

    def update_details(
        self,
        mission_id: str,
        *,
        source_name: str | None = None,
        total_frames: int | None = None,
        fps: float | None = None,
    ) -> Mission | None:
        mission = self._db.missions.get(mission_id)
        if mission is None:
            return None
        if source_name is not None:
            mission.source_name = source_name
        if total_frames is not None:
            mission.total_frames = total_frames
        if fps is not None:
            mission.fps = fps
        return mission

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


class InMemoryAlertRepository:
    allowed_target_statuses = {
        AlertStatus.REVIEWED_CONFIRMED,
        AlertStatus.REVIEWED_REJECTED,
    }

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
            alerts = [alert for alert in alerts if alert.status == status]
        return sorted(alerts, key=lambda alert: (alert.ts_sec, alert.frame_id))

    def update_status(
        self,
        alert_id: str,
        updates: AlertReviewPayload,
    ) -> Alert | None:
        alert = self._db.alerts.get(alert_id)
        if alert is None:
            return None
        status = updates["status"]
        if status not in self.allowed_target_statuses:
            raise ValueError("Invalid target status")
        if alert.status != AlertStatus.QUEUED:
            if alert.status == status:
                return alert
            raise ValueError("Alert already reviewed")

        alert.status = status
        alert.reviewed_by = updates.get("reviewed_by")
        reviewed_at = updates.get("reviewed_at_sec")
        alert.reviewed_at_sec = (
            float(reviewed_at) if reviewed_at is not None else alert.ts_sec
        )
        alert.decision_reason = updates.get("decision_reason")
        return alert


class InMemoryFrameEventRepository:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def add(self, frame_event: FrameEvent) -> None:
        frames = self._db.mission_frames.setdefault(frame_event.mission_id, [])
        for idx, existing in enumerate(frames):
            if existing.frame_id == frame_event.frame_id:
                frames[idx] = frame_event
                return
        frames.append(frame_event)

    def list_by_mission(self, mission_id: str) -> list[FrameEvent]:
        return list(self._db.mission_frames.get(mission_id, []))


@dataclass
class InMemoryArtifactStorage:
    stored_frames: dict[tuple[str, int], str] = field(default_factory=dict)
    _reports: dict[str, dict[str, object]] = field(default_factory=dict)

    def store_frame(self, mission_id: str, frame_id: int, source_uri: str) -> str:
        _ = source_uri
        uri = f"memory://missions/{mission_id}/frames/{frame_id}.jpg"
        self.stored_frames[(mission_id, frame_id)] = uri
        return uri

    def load_frame(self, image_uri: str) -> ArtifactBlob | None:
        if image_uri in self.stored_frames.values():
            return ArtifactBlob(
                content=b"",
                media_type="image/jpeg",
                filename=image_uri.split("/")[-1] or "frame.jpg",
            )
        return None

    def save_mission_report(self, mission_id: str, report: Mapping[str, object]) -> str:
        self._reports[mission_id] = dict(report)
        return f"memory://missions/{mission_id}/report.json"

    def load_mission_report(self, mission_id: str) -> Mapping[str, object] | None:
        payload = self._reports.get(mission_id)
        return dict(payload) if payload is not None else None

    def write_report(self, run_key: str, payload: dict[str, object]) -> str:
        self._reports[run_key] = dict(payload)
        return f"memory://batch/{run_key}/report.json"

    def write_debug_rows(self, run_key: str, rows: list[dict[str, object]]) -> str:
        _ = rows
        return f"memory://batch/{run_key}/debug.csv"
