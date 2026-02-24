import mimetypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from libs.core.application.pilot_service import DetectionInput
from libs.core.domain.entities import Alert, FrameEvent
from services.api_gateway.dependencies import get_pilot_service
from services.api_gateway.infrastructure.stream_runner import (
    build_stream_config,
    get_stream_state,
    start_stream,
)
from services.api_gateway.presentation.http.ui_page import build_ui_html

router = APIRouter()


class DetectionRequest(BaseModel):
    bbox: tuple[float, float, float, float]
    score: float = Field(ge=0.0, le=1.0)
    label: str = "person"
    model_name: str = "yolo8n"
    explanation: str | None = None


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


class MissionStartFlowRequest(BaseModel):
    source_name: str = "pilot-dataset"
    fps: float = Field(default=2.0, gt=0.0)
    frames_dir: str
    labels_dir: str | None = None
    high_score: float = Field(default=0.95, ge=0.0, le=1.0)
    low_score: float = Field(default=0.05, ge=0.0, le=1.0)
    api_base: str = "http://127.0.0.1:8000"


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


@router.post("/v1/missions/{mission_id}/complete")
def complete_mission(mission_id: str) -> dict[str, str]:
    service = get_pilot_service()
    mission = service.complete_mission(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    return {"mission_id": mission.mission_id, "status": mission.status}


@router.post("/v1/missions/start-flow")
def start_mission_flow(payload: MissionStartFlowRequest) -> dict[str, object]:
    service = get_pilot_service()
    mission = service.create_mission(
        source_name=payload.source_name,
        total_frames=0,
        fps=payload.fps,
    )

    try:
        config = build_stream_config(
            mission_id=mission.mission_id,
            options={
                "frames_dir": payload.frames_dir,
                "labels_dir": payload.labels_dir,
                "fps": payload.fps,
                "high_score": payload.high_score,
                "low_score": payload.low_score,
                "api_base": payload.api_base,
            },
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    mission.total_frames = len(config.frame_files)
    started = service.start_mission(mission.mission_id)
    if started is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    state = start_stream(config)

    return {
        "mission_id": mission.mission_id,
        "status": started.status,
        "created_at": mission.created_at,
        "source_name": mission.source_name,
        "fps": mission.fps,
        "total_frames": mission.total_frames,
        "stream": {
            "running": state.running,
            "processed_frames": state.processed_frames,
            "total_frames": state.total_frames,
            "last_frame_name": state.last_frame_name,
            "error": state.error,
        },
    }


@router.get("/v1/missions/{mission_id}/stream/status")
def get_mission_stream_status(mission_id: str) -> dict[str, object]:
    service = get_pilot_service()
    if service.get_mission(mission_id) is None:
        raise HTTPException(status_code=404, detail="Mission not found")

    state = get_stream_state(mission_id)
    if state is None:
        return {
            "mission_id": mission_id,
            "running": False,
            "processed_frames": 0,
            "total_frames": 0,
            "last_frame_name": None,
            "error": None,
        }
    return {
        "mission_id": state.mission_id,
        "running": state.running,
        "processed_frames": state.processed_frames,
        "total_frames": state.total_frames,
        "last_frame_name": state.last_frame_name,
        "error": state.error,
    }


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
            explanation=item.explanation,
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
    return [_alert_to_dict(alert, service=service) for alert in alerts]


@router.get("/v1/alerts/{alert_id}")
def get_alert_details(alert_id: str) -> dict[str, object]:
    service = get_pilot_service()
    alert = service.get_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    return _alert_to_dict(alert, service=service)


@router.get("/v1/alerts/{alert_id}/frame")
def get_alert_frame(alert_id: str) -> FileResponse:
    service = get_pilot_service()
    alert = service.get_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")

    frame_path = Path(alert.image_uri)
    if not frame_path.is_absolute():
        raise HTTPException(status_code=400, detail="Frame URI is not local path")
    if not frame_path.exists():
        raise HTTPException(status_code=404, detail="Frame file not found")

    media_type, _ = mimetypes.guess_type(frame_path.name)
    return FileResponse(
        path=frame_path,
        media_type=media_type or "application/octet-stream",
        filename=frame_path.name,
    )


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
    return _alert_to_dict(alert, service=service)


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
    return _alert_to_dict(alert, service=service)


@router.get("/v1/missions/{mission_id}/report")
def get_mission_report_endpoint(mission_id: str) -> dict[str, object]:
    service = get_pilot_service()
    try:
        return service.get_mission_report(mission_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def _alert_to_dict(alert: Alert, service: Any) -> dict[str, object]:
    mission = service.get_mission(alert.mission_id)
    wall_time = _build_alert_wall_time(
        created_at=mission.created_at if mission is not None else None,
        offset_sec=alert.ts_sec,
    )
    return {
        "alert_id": alert.alert_id,
        "mission_id": alert.mission_id,
        "frame_id": alert.frame_id,
        "ts_sec": alert.ts_sec,
        "alert_time_iso": wall_time,
        "image_uri": alert.image_uri,
        "people_detected": alert.evidence.people_detected,
        "bbox": list(alert.evidence.primary_detection.bbox),
        "score": alert.evidence.primary_detection.score,
        "label": alert.evidence.primary_detection.label,
        "model_name": alert.evidence.primary_detection.model_name,
        "explanation": alert.evidence.primary_detection.explanation,
        "status": alert.lifecycle.status,
        "reviewed_by": alert.lifecycle.reviewed_by,
        "reviewed_at_sec": alert.lifecycle.reviewed_at_sec,
        "decision_reason": alert.lifecycle.decision_reason,
    }


def _build_alert_wall_time(created_at: str | None, offset_sec: float) -> str | None:
    if created_at is None:
        return None
    try:
        base = datetime.fromisoformat(created_at)
    except ValueError:
        return None
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base + timedelta(seconds=offset_sec)).isoformat()
