from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from libs.core.application.pilot_service import DetectionInput
from libs.core.domain.entities import Alert, FrameEvent
from services.api_gateway.dependencies import get_pilot_service
from services.api_gateway.presentation.http.ui_page import build_ui_html

router = APIRouter()


class MissionCreateRequest(BaseModel):
    source_name: str = "pilot-dataset"
    total_frames: int = Field(default=0, ge=0)
    fps: float = Field(default=0.0, ge=0.0)


class DetectionRequest(BaseModel):
    bbox: tuple[float, float, float, float]
    score: float = Field(ge=0.0, le=1.0)
    label: str = "person"
    model_name: str = "yolo8n"


class FrameIngestRequest(BaseModel):
    frame_id: int = Field(ge=0)
    ts_sec: float = Field(ge=0.0)
    image_uri: str
    gt_person_present: bool
    gt_episode_id: str | None = None
    detections: list[DetectionRequest] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    reviewed_by: str | None = None
    reviewed_at_sec: float | None = Field(default=None, ge=0.0)
    decision_reason: str | None = None


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/", response_class=HTMLResponse)
def ui_index() -> str:
    return build_ui_html()


@router.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@router.get("/ready")
def ready() -> dict[str, str]:
    return {"status": "ready"}


@router.get("/version")
def version() -> dict[str, str]:
    return {"version": "0.1.0"}


@router.post("/v1/missions")
def create_mission_endpoint(payload: MissionCreateRequest) -> dict[str, object]:
    service = get_pilot_service()
    mission = service.create_mission(
        source_name=payload.source_name,
        total_frames=payload.total_frames,
        fps=payload.fps,
    )
    return {
        "mission_id": mission.mission_id,
        "source_name": mission.source_name,
        "status": mission.status,
        "created_at": mission.created_at,
        "total_frames": mission.total_frames,
        "fps": mission.fps,
    }


@router.post("/v1/missions/{mission_id}/start")
def start_mission(mission_id: str) -> dict[str, str]:
    service = get_pilot_service()
    mission = service.start_mission(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    return {"mission_id": mission.mission_id, "status": mission.status}


@router.post("/v1/missions/{mission_id}/complete")
def complete_mission(mission_id: str) -> dict[str, str]:
    service = get_pilot_service()
    mission = service.complete_mission(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    return {"mission_id": mission.mission_id, "status": mission.status}


@router.post("/v1/missions/{mission_id}/frames")
def ingest_frame_endpoint(
    mission_id: str,
    payload: FrameIngestRequest,
) -> dict[str, object]:
    service = get_pilot_service()
    detections = [
        DetectionInput(
            bbox=item.bbox,
            score=item.score,
            label=item.label,
            model_name=item.model_name,
        )
        for item in payload.detections
    ]
    frame_event = FrameEvent(
        mission_id=mission_id,
        frame_id=payload.frame_id,
        ts_sec=payload.ts_sec,
        image_uri=payload.image_uri,
        gt_person_present=payload.gt_person_present,
        gt_episode_id=payload.gt_episode_id,
    )

    try:
        alerts = service.ingest_frame_event(
            frame_event=frame_event,
            detections=detections,
        )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    return {
        "mission_id": mission_id,
        "frame_id": payload.frame_id,
        "accepted": True,
        "alerts_created": len(alerts),
        "alert_ids": [alert.alert_id for alert in alerts],
    }


@router.get("/v1/alerts")
def get_alerts(
    mission_id: str | None = None,
    status: str | None = None,
) -> list[dict[str, object]]:
    service = get_pilot_service()
    alerts = service.list_alerts(mission_id=mission_id, status=status)
    return [_alert_to_dict(alert) for alert in alerts]


@router.get("/v1/alerts/{alert_id}")
def get_alert_details(alert_id: str) -> dict[str, object]:
    service = get_pilot_service()
    alert = service.get_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    return _alert_to_dict(alert)


@router.post("/v1/alerts/{alert_id}/confirm")
def confirm_alert(alert_id: str, payload: ReviewRequest) -> dict[str, object]:
    service = get_pilot_service()
    try:
        alert = service.review_alert(
            alert_id=alert_id,
            decision={
                "status": "reviewed_confirmed",
                "reviewed_by": payload.reviewed_by,
                "reviewed_at_sec": payload.reviewed_at_sec,
                "decision_reason": payload.decision_reason,
            },
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    return _alert_to_dict(alert)


@router.post("/v1/alerts/{alert_id}/reject")
def reject_alert(alert_id: str, payload: ReviewRequest) -> dict[str, object]:
    service = get_pilot_service()
    try:
        alert = service.review_alert(
            alert_id=alert_id,
            decision={
                "status": "reviewed_rejected",
                "reviewed_by": payload.reviewed_by,
                "reviewed_at_sec": payload.reviewed_at_sec,
                "decision_reason": payload.decision_reason,
            },
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    return _alert_to_dict(alert)


@router.get("/v1/missions/{mission_id}/report")
def get_mission_report_endpoint(mission_id: str) -> dict[str, object]:
    service = get_pilot_service()
    try:
        return service.get_mission_report(mission_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def _alert_to_dict(alert: Alert) -> dict[str, object]:
    return {
        "alert_id": alert.alert_id,
        "mission_id": alert.mission_id,
        "frame_id": alert.frame_id,
        "ts_sec": alert.ts_sec,
        "image_uri": alert.image_uri,
        "bbox": list(alert.detection.bbox),
        "score": alert.detection.score,
        "label": alert.detection.label,
        "model_name": alert.detection.model_name,
        "status": alert.lifecycle.status,
        "reviewed_by": alert.lifecycle.reviewed_by,
        "reviewed_at_sec": alert.lifecycle.reviewed_at_sec,
        "decision_reason": alert.lifecycle.decision_reason,
    }
