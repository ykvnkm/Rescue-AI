from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.api_gateway.infrastructure.memory_store import (
    create_mission,
    list_alerts,
    update_alert_status,
)

router = APIRouter()


class ReviewRequest(BaseModel):
    reviewed_by: str | None = None


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
def ready() -> dict[str, str]:
    return {"status": "ready"}


@router.get("/version")
def version() -> dict[str, str]:
    return {"version": "0.1.0"}


@router.post("/v1/missions")
def create_mission_endpoint() -> dict[str, str]:
    mission = create_mission()
    return {
        "mission_id": mission.mission_id,
        "status": mission.status,
    }


@router.get("/v1/alerts")
def get_alerts(mission_id: str | None = None) -> list[dict[str, object]]:
    alerts = list_alerts(mission_id=mission_id)
    return [
        {
            "alert_id": alert.alert_id,
            "mission_id": alert.mission_id,
            "frame_id": alert.frame_id,
            "ts_sec": alert.ts_sec,
            "score": alert.score,
            "status": alert.status,
            "reviewed_by": alert.reviewed_by,
        }
        for alert in alerts
    ]


@router.post("/v1/alerts/{alert_id}/confirm")
def confirm_alert(alert_id: str, payload: ReviewRequest) -> dict[str, object]:
    alert = update_alert_status(
        alert_id=alert_id,
        status="reviewed_confirmed",
        reviewed_by=payload.reviewed_by,
    )
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {
        "alert_id": alert.alert_id,
        "status": alert.status,
        "reviewed_by": alert.reviewed_by,
    }


@router.post("/v1/alerts/{alert_id}/reject")
def reject_alert(alert_id: str, payload: ReviewRequest) -> dict[str, object]:
    alert = update_alert_status(
        alert_id=alert_id,
        status="reviewed_rejected",
        reviewed_by=payload.reviewed_by,
    )
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {
        "alert_id": alert.alert_id,
        "status": alert.status,
        "reviewed_by": alert.reviewed_by,
    }
