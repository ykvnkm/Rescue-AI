"""FastAPI route handlers for Rescue-AI API."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from rescue_ai.config import get_settings
from rescue_ai.domain.entities import Alert, Detection, FrameEvent
from rescue_ai.domain.ports import AlertReviewPayload
from rescue_ai.interfaces.api.dependencies import (
    get_detector,
    get_pilot_service,
    get_stream_controller,
)
from rescue_ai.interfaces.api.ui_page import build_ui_html

logger = logging.getLogger(__name__)
router = APIRouter()
DEFAULT_STREAM_FPS = 2.0


# ── Request / Response models ──────────────────────────────────────


class DetectionRequest(BaseModel):
    """Single detection item in frame ingestion payload."""

    bbox: tuple[float, float, float, float]
    score: float = Field(ge=0.0, le=1.0)
    label: str = "person"
    model_name: str = "yolo8n"
    explanation: str | None = None


class FrameIngestRequest(BaseModel):
    """Frame ingestion request with detections and ground truth."""

    frame_id: int = Field(ge=0)
    ts_sec: float = Field(ge=0.0)
    image_uri: str
    gt_person_present: bool
    gt_episode_id: str | None = None
    detections: list[DetectionRequest] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    """Alert review decision from operator."""

    reviewed_by: str | None = None
    reviewed_at_sec: float | None = Field(default=None, ge=0.0)
    decision_reason: str | None = None


class MissionStartRequest(BaseModel):
    """Request to create and start a mission with RPi stream."""

    rpi_mission_id: str = Field(description="Mission ID on Raspberry Pi")
    fps: float = Field(default=DEFAULT_STREAM_FPS, gt=0.0)


class PredictRequest(BaseModel):
    """Single-frame detection request (server-side YOLO)."""

    image_uri: str = Field(description="Path or URL to the image")


class DetectionResponse(BaseModel):
    """Single detection result returned by /predict endpoint."""

    bbox: tuple[float, float, float, float]
    score: float
    label: str
    model_name: str


class PredictResponse(BaseModel):
    """Aggregated detection results for a single image."""

    image_uri: str
    detections: list[DetectionResponse]
    count: int


class ForceCompleteRequest(BaseModel):
    """Emergency mission completion request."""

    reviewed_by: str = "авто-обход"
    decision_reason: str = "аварийное снятие зависшей очереди"


def _resolve_queued_alerts_for_force_complete(
    *,
    service: Any,
    mission_id: str,
    payload: ForceCompleteRequest,
) -> tuple[int, list[str]]:
    queued_alerts = service.list_alerts(mission_id=mission_id, status="queued")
    failed_alert_ids: list[str] = []
    resolved_count = 0
    for alert in queued_alerts:
        review_payload = cast(
            AlertReviewPayload,
            {
                "status": "reviewed_rejected",
                "reviewed_by": payload.reviewed_by,
                "reviewed_at_sec": alert.ts_sec,
                "decision_reason": payload.decision_reason,
            },
        )
        try:
            reviewed = service.review_alert(alert.alert_id, review_payload)
            if reviewed is None:
                failed_alert_ids.append(alert.alert_id)
            else:
                resolved_count += 1
        except ValueError:
            failed_alert_ids.append(alert.alert_id)
    return resolved_count, failed_alert_ids


# ── System endpoints ───────────────────────────────────────────────


@router.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", tags=["system"])
def ready() -> dict[str, object]:
    settings = get_settings()
    checks = {
        "db_dsn_configured": bool(settings.database.dsn.strip()),
        "s3_bucket_configured": bool(settings.storage.s3_bucket.strip()),
        "s3_access_key_configured": bool(settings.storage.s3_access_key_id.strip()),
        "s3_secret_key_configured": bool(settings.storage.s3_secret_access_key.strip()),
        "rpi_base_url_configured": bool(settings.rpi.base_url.strip()),
        "rpi_rtsp_port_configured": settings.rpi.rtsp_port > 0,
    }
    if not all(checks.values()):
        raise HTTPException(
            status_code=503, detail={"status": "not_ready", "checks": checks}
        )
    return {"status": "ready", "checks": checks}


@router.get("/rpi/status", tags=["system"])
def rpi_status() -> dict[str, object]:
    settings = get_settings()
    base_url = settings.rpi.base_url
    if not base_url:
        return {"connected": False, "base_url": "", "detail": "RPI_BASE_URL not set"}

    stream_controller = get_stream_controller()
    try:
        stream_controller.check_rpi_health()
        return {"connected": True, "base_url": base_url}
    except (ValueError, RuntimeError, OSError) as error:
        return {
            "connected": False,
            "base_url": base_url,
            "detail": f"{type(error).__name__}: {error}",
        }


@router.get("/rpi/missions", tags=["system"])
def rpi_missions() -> dict[str, object]:
    stream_controller = get_stream_controller()
    try:
        missions = stream_controller.list_rpi_missions()
    except (ValueError, RuntimeError, OSError) as error:
        raise HTTPException(
            status_code=503,
            detail=f"RPi catalog fetch failed: {type(error).__name__}: {error}",
        ) from error
    return {"missions": [item for item in missions if item.get("mission_id", "")]}


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


# ── Operator UI ────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def ui_index() -> str:
    return build_ui_html()


@router.get("/pilot", response_class=HTMLResponse, include_in_schema=False)
def pilot_ui() -> str:
    return build_ui_html()


# ── Missions ───────────────────────────────────────────────────────


@router.post("/missions/start", tags=["missions"])
def start_mission(payload: MissionStartRequest) -> dict[str, object]:
    service = get_pilot_service()
    stream_controller = get_stream_controller()
    detector = get_detector()
    if detector is None:
        raise HTTPException(
            status_code=503,
            detail="Detector not available (model not loaded)",
        )
    active_mission = service.get_active_mission()
    if active_mission is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Active mission exists: {active_mission.mission_id} "
                f"({active_mission.status})"
            ),
        )
    launch_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

    mission = service.create_mission(
        source_name=f"rpi:{payload.rpi_mission_id}:{launch_tag}",
        total_frames=0,
        fps=payload.fps,
    )
    started = service.start_mission(mission.mission_id)
    if started is None:
        raise HTTPException(status_code=404, detail="Mission not found")

    try:
        stream_controller.start(
            mission_id=mission.mission_id,
            rpi_mission_id=payload.rpi_mission_id,
            target_fps=payload.fps,
        )
    except ValueError as error:
        error_text = str(error)
        if "not found" in error_text.lower():
            raise HTTPException(status_code=404, detail=error_text) from error
        raise HTTPException(status_code=409, detail=error_text) from error
    except (RuntimeError, OSError) as error:
        raise HTTPException(
            status_code=503,
            detail=f"RPi stream start failed: {type(error).__name__}: {error}",
        ) from error

    logger.info(
        "Mission started: mission_id=%s source=%s fps=%.1f",
        mission.mission_id,
        mission.source_name,
        mission.fps,
    )
    return {
        "mission_id": mission.mission_id,
        "status": started.status,
        "source_name": mission.source_name,
        "fps": mission.fps,
        "stream": stream_controller.as_payload(mission.mission_id),
    }


@router.post("/missions/{mission_id}/stop-stream", tags=["missions"])
def stop_mission_stream(mission_id: str) -> dict[str, object]:
    """Stop the video stream but keep mission open for alert review."""
    service = get_pilot_service()
    stream_controller = get_stream_controller()

    mission = service.get_mission(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    if mission.status != "running":
        raise HTTPException(status_code=409, detail="Mission is not running")

    stopped_state = stream_controller.stop(mission_id)
    queued = service.list_alerts(mission_id=mission_id, status="queued")

    return {
        "mission_id": mission_id,
        "status": mission.status,
        "stream_stopped": True,
        "queued_alerts": len(queued),
        "processed_frames": (
            stopped_state.processed_frames if stopped_state is not None else None
        ),
    }


@router.post("/missions/{mission_id}/complete", tags=["missions"])
def complete_mission(mission_id: str) -> dict[str, object]:
    service = get_pilot_service()
    stream_controller = get_stream_controller()

    if service.get_mission(mission_id) is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    queued_alerts = service.list_alerts(mission_id=mission_id, status="queued")
    if queued_alerts:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot complete mission with queued alerts: {len(queued_alerts)}",
        )

    stopped_state = stream_controller.stop(mission_id)
    completed_frame_id = None
    if stopped_state is not None and stopped_state.processed_frames > 0:
        completed_frame_id = stopped_state.processed_frames - 1

    try:
        mission = service.complete_mission(
            mission_id=mission_id,
            completed_frame_id=completed_frame_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f"Artifact/report storage error: {type(error).__name__}: {error}",
        ) from error
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")

    try:
        report = service.get_mission_report(mission_id)
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f"Artifact/report storage error: {type(error).__name__}: {error}",
        ) from error

    logger.info(
        "Mission completed: mission_id=%s completed_frame=%s",
        mission.mission_id,
        mission.completed_frame_id,
    )
    return {
        "mission_id": mission.mission_id,
        "status": mission.status,
        "completed_frame_id": mission.completed_frame_id,
        "end_reason": (
            None
            if stopped_state is None
            else stopped_state.error or stopped_state.end_reason
        ),
        "report": report,
    }


@router.post("/missions/{mission_id}/force-complete", tags=["missions"])
def force_complete_mission(
    mission_id: str,
    payload: ForceCompleteRequest = ForceCompleteRequest(),
) -> dict[str, object]:
    """Emergency path: resolve queued alerts and complete the mission."""
    service = get_pilot_service()
    stream_controller = get_stream_controller()

    mission = service.get_mission(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    if mission.status == "completed":
        return {
            "mission_id": mission.mission_id,
            "status": mission.status,
            "completed_frame_id": mission.completed_frame_id,
            "end_reason": "already_completed",
            "resolved_queued_alerts": 0,
            "failed_queued_alerts": [],
            "report": service.get_mission_report(mission_id),
        }

    stopped_state = stream_controller.stop(mission_id)
    completed_frame_id = None
    if stopped_state is not None and stopped_state.processed_frames > 0:
        completed_frame_id = stopped_state.processed_frames - 1

    resolved_count, failed_alert_ids = _resolve_queued_alerts_for_force_complete(
        service=service,
        mission_id=mission_id,
        payload=payload,
    )

    if failed_alert_ids:
        raise HTTPException(
            status_code=409,
            detail=(
                "Failed to resolve queued alerts before completion: "
                f"{', '.join(failed_alert_ids)}"
            ),
        )

    try:
        completed = service.complete_mission(
            mission_id=mission_id,
            completed_frame_id=completed_frame_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f"Artifact/report storage error: {type(error).__name__}: {error}",
        ) from error
    if completed is None:
        raise HTTPException(status_code=404, detail="Mission not found")

    try:
        report = service.get_mission_report(mission_id)
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f"Artifact/report storage error: {type(error).__name__}: {error}",
        ) from error

    logger.info(
        "Mission force-completed: mission_id=%s resolved_alerts=%d",
        completed.mission_id,
        resolved_count,
    )
    return {
        "mission_id": completed.mission_id,
        "status": completed.status,
        "completed_frame_id": completed.completed_frame_id,
        "end_reason": (
            None
            if stopped_state is None
            else stopped_state.error or stopped_state.end_reason
        ),
        "resolved_queued_alerts": resolved_count,
        "failed_queued_alerts": failed_alert_ids,
        "report": report,
    }


@router.get("/missions/{mission_id}/stream/status", tags=["missions"])
def get_mission_stream_status(mission_id: str) -> dict[str, object]:
    service = get_pilot_service()
    stream_controller = get_stream_controller()

    if service.get_mission(mission_id) is None:
        raise HTTPException(status_code=404, detail="Mission not found")

    payload = stream_controller.as_payload(mission_id)
    if payload is None:
        return {
            "mission_id": mission_id,
            "running": False,
            "session_id": None,
            "rtsp_url": None,
            "error": None,
            "last_accounted_frame_id": None,
        }
    processed = payload.get("processed_frames")
    last_accounted_frame_id = None
    if isinstance(processed, int) and processed > 0:
        last_accounted_frame_id = processed - 1
    return {
        "mission_id": mission_id,
        "last_accounted_frame_id": last_accounted_frame_id,
        **payload,
    }


@router.get("/missions/{mission_id}/report", tags=["missions"])
def get_mission_report(mission_id: str) -> dict[str, object]:
    service = get_pilot_service()
    try:
        return service.get_mission_report(mission_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f"Artifact/report storage error: {type(error).__name__}: {error}",
        ) from error


# ── Frame ingestion ────────────────────────────────────────────────


@router.post("/missions/{mission_id}/frames", tags=["frames"])
def ingest_frame(
    mission_id: str,
    payload: FrameIngestRequest,
) -> dict[str, object]:
    service = get_pilot_service()
    detections = [
        Detection(
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
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f"Artifact storage error: {type(error).__name__}: {error}",
        ) from error

    logger.info(
        "Frame ingested: mission=%s frame=%d detections=%d alerts_created=%d",
        mission_id,
        payload.frame_id,
        len(payload.detections),
        len(alerts),
    )
    return {
        "mission_id": mission_id,
        "frame_id": payload.frame_id,
        "accepted": True,
        "alerts_created": len(alerts),
        "alert_ids": [alert.alert_id for alert in alerts],
    }


# ── Alerts ─────────────────────────────────────────────────────────


@router.get("/alerts", tags=["alerts"])
def get_alerts(
    mission_id: str | None = None,
    status: str | None = None,
) -> list[dict[str, object]]:
    service = get_pilot_service()
    if mission_id is not None and service.get_mission(mission_id) is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    alerts = service.list_alerts(mission_id=mission_id, status=status)
    return [_alert_to_dict(alert, service=service) for alert in alerts]


@router.get("/alerts/{alert_id}", tags=["alerts"])
def get_alert_details(alert_id: str) -> dict[str, object]:
    service = get_pilot_service()
    alert = service.get_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    return _alert_to_dict(alert, service=service)


@router.get("/alerts/{alert_id}/frame", tags=["alerts"])
def get_alert_frame(alert_id: str) -> Response:
    service = get_pilot_service()
    try:
        artifact = service.get_alert_frame_artifact(alert_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f"Artifact storage error: {type(error).__name__}: {error}",
        ) from error

    return Response(
        content=artifact.content,
        media_type=artifact.media_type,
        headers={"Content-Disposition": f'inline; filename="{artifact.filename}"'},
    )


@router.post("/alerts/{alert_id}/confirm", tags=["alerts"])
def confirm_alert(alert_id: str, payload: ReviewRequest) -> dict[str, object]:
    service = get_pilot_service()
    review_payload = cast(
        AlertReviewPayload,
        {
            "status": "reviewed_confirmed",
            "reviewed_by": payload.reviewed_by,
            "reviewed_at_sec": payload.reviewed_at_sec,
            "decision_reason": payload.decision_reason,
        },
    )
    try:
        alert = service.review_alert(alert_id, review_payload)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    logger.info(
        "Alert confirmed: alert_id=%s mission=%s reviewed_by=%s",
        alert_id,
        alert.mission_id,
        payload.reviewed_by,
    )
    return _alert_to_dict(alert, service=service)


@router.post("/alerts/{alert_id}/reject", tags=["alerts"])
def reject_alert(alert_id: str, payload: ReviewRequest) -> dict[str, object]:
    service = get_pilot_service()
    review_payload = cast(
        AlertReviewPayload,
        {
            "status": "reviewed_rejected",
            "reviewed_by": payload.reviewed_by,
            "reviewed_at_sec": payload.reviewed_at_sec,
            "decision_reason": payload.decision_reason,
        },
    )
    try:
        alert = service.review_alert(alert_id, review_payload)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    logger.info(
        "Alert rejected: alert_id=%s mission=%s reviewed_by=%s",
        alert_id,
        alert.mission_id,
        payload.reviewed_by,
    )
    return _alert_to_dict(alert, service=service)


# ── Single-frame detection (server-side YOLO) ─────────────────────


@router.post("/predict", response_model=PredictResponse, tags=["detection"])
def predict(payload: PredictRequest) -> PredictResponse:
    logger.info("Predict request: image_uri=%s", payload.image_uri)
    detector = get_detector()
    if detector is None:
        raise HTTPException(
            status_code=503,
            detail="Detector not available (model not loaded)",
        )

    t0 = time.monotonic()
    try:
        detections = detector.detect(payload.image_uri)
    except Exception as error:
        logger.error(
            "Predict failed: image_uri=%s error=%s: %s",
            payload.image_uri,
            type(error).__name__,
            error,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Detection failed: {type(error).__name__}: {error}",
        ) from error
    inference_ms = (time.monotonic() - t0) * 1000

    for d in detections:
        logger.info(
            "  Detection found: label=%s score=%.3f "
            "bbox=[%.1f,%.1f,%.1f,%.1f] model=%s",
            d.label,
            d.score,
            *d.bbox,
            d.model_name,
        )
    logger.info(
        "Predict complete: image_uri=%s detections=%d inference_ms=%.1f",
        payload.image_uri,
        len(detections),
        inference_ms,
    )

    return PredictResponse(
        image_uri=payload.image_uri,
        detections=[
            DetectionResponse(
                bbox=d.bbox,
                score=d.score,
                label=d.label,
                model_name=d.model_name,
            )
            for d in detections
        ],
        count=len(detections),
    )


# ── Helpers ────────────────────────────────────────────────────────


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
        "people_detected": alert.people_detected,
        "bbox": list(alert.primary_detection.bbox),
        "bboxes": [list(item.bbox) for item in alert.detections],
        "scores": [item.score for item in alert.detections],
        "score": alert.primary_detection.score,
        "label": alert.primary_detection.label,
        "model_name": alert.primary_detection.model_name,
        "explanation": alert.primary_detection.explanation,
        "status": alert.status,
        "reviewed_by": alert.reviewed_by,
        "reviewed_at_sec": alert.reviewed_at_sec,
        "decision_reason": alert.decision_reason,
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
