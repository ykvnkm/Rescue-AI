"""In-memory repository implementations for testing."""

from dataclasses import dataclass, field

from rescue_ai.domain.entities import Alert, FrameEvent, Mission
from rescue_ai.domain.ports import AlertReviewPayload, ArtifactBlob


@dataclass
class InMemoryDatabase:
    """In-memory storage for missions, alerts and frame events."""

    missions: dict[str, Mission] = field(default_factory=dict)
    alerts: dict[str, Alert] = field(default_factory=dict)
    mission_frames: dict[str, list[FrameEvent]] = field(default_factory=dict)


class InMemoryMissionRepository:
    """Mission repository implementation over the in-memory database."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def create(self, mission: Mission) -> None:
        self._db.missions[mission.mission_id] = mission
        self._db.mission_frames.setdefault(mission.mission_id, [])

    def get(self, mission_id: str) -> Mission | None:
        return self._db.missions.get(mission_id)

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
    """Alert repository implementation over the in-memory database."""

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
            alerts = [alert for alert in alerts if alert.status == status]
        return sorted(alerts, key=lambda alert: (alert.ts_sec, alert.frame_id))

    def update_status(
        self,
        alert_id: str,
        updates: AlertReviewPayload,
    ) -> Alert | None:
        """Apply a review decision to an alert."""
        alert = self._db.alerts.get(alert_id)
        if alert is None:
            return None
        status = str(updates.get("status", ""))
        if status not in self.allowed_target_statuses:
            raise ValueError("Invalid target status")
        if alert.status != "queued":
            raise ValueError("Alert already reviewed")

        alert.status = status
        reviewed_by = updates.get("reviewed_by")
        alert.reviewed_by = reviewed_by if isinstance(reviewed_by, str) else None
        reviewed_at = updates.get("reviewed_at_sec")
        if reviewed_at is None:
            alert.reviewed_at_sec = alert.ts_sec
        elif isinstance(reviewed_at, (int, float, str)):
            alert.reviewed_at_sec = float(reviewed_at)
        else:
            raise ValueError("Invalid reviewed_at_sec")
        decision_reason = updates.get("decision_reason")
        alert.decision_reason = (
            decision_reason if isinstance(decision_reason, str) else None
        )
        return alert


class InMemoryFrameEventRepository:
    """Frame event repository implementation over the in-memory database."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def add(self, frame_event: FrameEvent) -> None:
        self._db.mission_frames.setdefault(frame_event.mission_id, []).append(
            frame_event
        )

    def list_by_mission(self, mission_id: str) -> list[FrameEvent]:
        return list(self._db.mission_frames.get(mission_id, []))


@dataclass
class InMemoryArtifactStorage:
    """In-memory artifact storage used by batch pilot engine and tests."""

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

    def save_mission_report(self, mission_id: str, report: dict[str, object]) -> str:
        self._reports[mission_id] = dict(report)
        return f"memory://missions/{mission_id}/report.json"

    def load_mission_report(self, mission_id: str) -> dict[str, object] | None:
        payload = self._reports.get(mission_id)
        return dict(payload) if payload is not None else None

    def write_report(self, run_key: str, payload: dict[str, object]) -> str:
        """Store a batch run report in memory."""
        self._reports[run_key] = dict(payload)
        return f"memory://batch/{run_key}/report.json"

    def write_debug_rows(self, run_key: str, rows: list[dict[str, object]]) -> str:
        """Store batch debug rows (no-op for in-memory, returns URI)."""
        _ = rows
        return f"memory://batch/{run_key}/debug.csv"
