"""In-memory storage for mission and alert flow (MVP)."""

from uuid import uuid4

from libs.core.domain.entities import Alert, Mission

MISSIONS: dict[str, Mission] = {}
ALERTS: dict[str, Alert] = {}


def reset_state() -> None:
    """Reset in-memory state for tests."""
    MISSIONS.clear()
    ALERTS.clear()


def create_mission() -> Mission:
    """Create a mission in created state."""
    mission = Mission(mission_id=str(uuid4()), status="created")
    MISSIONS[mission.mission_id] = mission
    return mission


def add_alert(
    mission_id: str,
    frame_id: int,
    ts_sec: float,
    score: float,
) -> Alert:
    """Create and persist an alert for a mission."""
    alert = Alert(
        alert_id=str(uuid4()),
        mission_id=mission_id,
        frame_id=frame_id,
        ts_sec=ts_sec,
        score=score,
        status="queued",
    )
    ALERTS[alert.alert_id] = alert
    return alert


def list_alerts(mission_id: str | None = None) -> list[Alert]:
    """List alerts, optionally filtered by mission."""
    alerts = list(ALERTS.values())
    if mission_id is None:
        return alerts
    return [alert for alert in alerts if alert.mission_id == mission_id]


def update_alert_status(
    alert_id: str,
    status: str,
    reviewed_by: str | None,
) -> Alert | None:
    """Update alert status and reviewer info."""
    alert = ALERTS.get(alert_id)
    if alert is None:
        return None
    alert.status = status
    alert.reviewed_by = reviewed_by
    return alert
