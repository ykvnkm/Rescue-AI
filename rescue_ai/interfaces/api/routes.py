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
from rescue_ai.domain.entities import Alert, Detection
from rescue_ai.domain.ports import AlertReviewPayload
from rescue_ai.interfaces.api.dependencies import (
    get_artifact_storage,
    get_detector,
    get_pilot_service,
    get_stream_controller,
)
from rescue_ai.interfaces.api.ui_page import build_ui_html

logger = logging.getLogger(__name__)
router = APIRouter()
DEFAULT_STREAM_FPS = 2.0


# ── Request / Response models ──────────────────────────────────────


class ReviewRequest(BaseModel):
    """Operator review decision for an alert."""

    reviewed_by: str = Field(
        default="оператор",
        description="Identifier of the reviewer",
    )


class MissionStartRequest(BaseModel):
    """Request to start a new mission with RPi video stream."""

    rpi_mission_id: str = Field(
        description="Mission identifier on Raspberry Pi device",
    )
    fps: float = Field(
        default=DEFAULT_STREAM_FPS,
        gt=0.0,
        description="Target frame processing rate (frames per second)",
    )


class PredictRequest(BaseModel):
    """Single-frame detection request using server-side YOLO model."""

    image_uri: str = Field(
        description=(
            "Image source: local file path or S3 URI "
            "(e.g. s3://bucket/path/frame.jpg)"
        ),
    )


class DetectionResponse(BaseModel):
    """Single detected object in the image."""

    bbox: tuple[float, float, float, float] = Field(
        description="Bounding box coordinates [x1, y1, x2, y2]",
    )
    score: float = Field(
        description="Detection confidence score (0.0 to 1.0)",
    )
    label: str = Field(description="Detected object class")
    model_name: str = Field(description="Model that produced this detection")


class PredictResponse(BaseModel):
    """Detection results for a single image."""

    image_uri: str = Field(description="Source image that was analyzed")
    detections: list[DetectionResponse] = Field(
        description="List of detected objects",
    )
    count: int = Field(description="Total number of detections")


class ForceCompleteRequest(BaseModel):
    """Emergency mission completion: auto-rejects all pending alerts."""

    reviewed_by: str = Field(
        default="авто-обход",
        description="Reviewer identifier for auto-rejected alerts",
    )
    decision_reason: str = Field(
        default="аварийное снятие зависшей очереди",
        description="Reason recorded for each auto-rejected alert",
    )


# ── Response models ──────────────────────────────────────────────


class HealthResponse(BaseModel):
    """Liveness probe response."""

    status: str = Field(description="Service status", examples=["ok"])


class ReadyResponse(BaseModel):
    """Readiness probe response with per-subsystem checks."""

    status: str = Field(description="Overall readiness", examples=["ready"])
    checks: dict[str, bool] = Field(
        description="Per-subsystem configuration checks " "(database, storage, rpi)",
    )


class RpiStatusResponse(BaseModel):
    """RPi device connectivity status."""

    connected: bool = Field(description="Whether RPi is reachable")


class MissionStartResponse(BaseModel):
    """Response after successfully starting a mission."""

    mission_id: str = Field(description="Unique mission identifier")
    status: str = Field(description="Mission status", examples=["running"])
    source_name: str = Field(description="Data source descriptor")
    fps: float = Field(description="Configured frame rate")
    stream: dict[str, object] | None = Field(
        description="Stream state snapshot",
    )


class StopStreamResponse(BaseModel):
    """Response after stopping the video stream."""

    mission_id: str = Field(description="Mission identifier")
    status: str = Field(description="Mission status")
    stream_stopped: bool = Field(description="Always true on success")
    queued_alerts: int = Field(
        description="Number of alerts awaiting operator review",
    )
    processed_frames: int | None = Field(
        description="Total frames processed before stop",
    )


class MissionCompleteResponse(BaseModel):
    """Response after completing a mission."""

    mission_id: str = Field(description="Mission identifier")
    status: str = Field(description="Mission status", examples=["completed"])
    completed_frame_id: int | None = Field(
        description="Last processed frame index",
    )
    end_reason: str | None = Field(
        description="Stream termination reason if applicable",
    )
    report: dict[str, object] = Field(
        description="Quality report with KPI metrics",
    )


class ForceCompleteResponse(MissionCompleteResponse):
    """Response after force-completing a mission."""

    resolved_queued_alerts: int = Field(
        description="Number of auto-rejected alerts",
    )
    failed_queued_alerts: list[str] = Field(
        description="Alert IDs that could not be resolved",
    )


class AlertResponse(BaseModel):
    """Alert details returned by list and detail endpoints."""

    alert_id: str = Field(description="Unique alert identifier")
    mission_id: str = Field(description="Parent mission identifier")
    frame_id: int = Field(description="Frame index that triggered alert")
    ts_sec: float = Field(description="Timestamp offset in seconds")
    alert_time_iso: str | None = Field(
        description="Absolute alert time in ISO 8601",
    )
    has_frame: bool = Field(
        description="Whether the original frame is available for download",
    )
    people_detected: int = Field(description="Number of people detected")
    bbox: list[float] = Field(
        description="Primary detection bounding box [x1, y1, x2, y2]",
    )
    bboxes: list[list[float]] = Field(
        description="All detection bounding boxes",
    )
    scores: list[float] = Field(
        description="Confidence scores for all detections",
    )
    score: float = Field(description="Primary detection confidence")
    label: str = Field(description="Detected class", examples=["person"])
    model_name: str = Field(description="Model name", examples=["yolo8n"])
    explanation: str | None = Field(
        description="Optional model explanation",
    )
    status: str = Field(
        description="Review status",
        examples=["queued", "reviewed_confirmed", "reviewed_rejected"],
    )
    reviewed_by: str | None = Field(
        description="Reviewer identifier",
    )


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


@router.get(
    "/health",
    tags=["system"],
    summary="Liveness check",
    response_model=HealthResponse,
)
def health() -> dict[str, str]:
    """Returns 200 if the service process is running."""
    return {"status": "ok"}


@router.get(
    "/ready",
    tags=["system"],
    summary="Readiness check",
    response_model=ReadyResponse,
    responses={503: {"description": "One or more subsystems not configured"}},
)
def ready() -> dict[str, object]:
    """Checks that all required integrations (database, S3, RPi) are configured.

    Returns 503 with a per-subsystem breakdown if any check fails."""
    settings = get_settings()
    checks = {
        "database": bool(settings.database.dsn.strip()),
        "storage": bool(
            settings.storage.s3_bucket.strip()
            and settings.storage.s3_access_key_id.strip()
        ),
        "rpi": bool(settings.rpi.base_url.strip() and settings.rpi.rtsp_port > 0),
    }
    if not all(checks.values()):
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "checks": checks},
        )
    return {"status": "ready", "checks": checks}


@router.get(
    "/rpi/status",
    tags=["system"],
    summary="RPi connectivity",
    response_model=RpiStatusResponse,
)
def rpi_status() -> dict[str, object]:
    """Ping the Raspberry Pi device and report whether it is reachable."""
    settings = get_settings()
    base_url = settings.rpi.base_url
    if not base_url:
        return {"connected": False}

    stream_controller = get_stream_controller()
    try:
        stream_controller.check_rpi_health()
        return {"connected": True}
    except (ValueError, RuntimeError, OSError) as error:
        logger.warning("RPi health check failed: %s: %s", type(error).__name__, error)
        return {"connected": False}


@router.get(
    "/rpi/missions",
    tags=["system"],
    summary="List RPi missions",
    responses={503: {"description": "RPi device unavailable"}},
)
def rpi_missions() -> dict[str, object]:
    """Fetch the catalog of recorded missions available on the RPi device."""
    stream_controller = get_stream_controller()
    try:
        missions = stream_controller.list_rpi_missions()
    except (ValueError, RuntimeError, OSError) as error:
        logger.warning("RPi catalog fetch failed: %s: %s", type(error).__name__, error)
        raise HTTPException(
            status_code=503, detail="RPi catalog unavailable"
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


@router.post(
    "/missions/start",
    tags=["missions"],
    summary="Start a new mission",
    response_model=MissionStartResponse,
    responses={
        404: {"description": "RPi mission not found"},
        409: {"description": "Another mission is already running"},
        503: {"description": "Detector or RPi stream unavailable"},
    },
)
def start_mission(payload: MissionStartRequest) -> dict[str, object]:
    """Create a mission, connect to the RPi video stream, and begin
    real-time person detection with YOLOv8."""
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
        logger.error("RPi stream start failed: %s: %s", type(error).__name__, error)
        raise HTTPException(status_code=503, detail="RPi stream unavailable") from error

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


@router.post(
    "/missions/{mission_id}/stop-stream",
    tags=["missions"],
    summary="Stop video stream",
    response_model=StopStreamResponse,
    responses={
        404: {"description": "Mission not found"},
        409: {"description": "Mission is not running"},
    },
)
def stop_mission_stream(mission_id: str) -> dict[str, object]:
    """Stop the RPi video stream but keep the mission open so the
    operator can continue reviewing pending alerts."""
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


@router.post(
    "/missions/{mission_id}/complete",
    tags=["missions"],
    summary="Complete mission",
    response_model=MissionCompleteResponse,
    responses={
        404: {"description": "Mission not found"},
        409: {"description": "Unreviewed alerts remain or already completed"},
        502: {"description": "Storage operation failed"},
    },
)
def complete_mission(mission_id: str) -> dict[str, object]:
    """Finalize the mission, stop the stream if still running, generate
    the quality report, and upload it to S3.

    Fails with 409 if there are unreviewed (queued) alerts."""
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
        logger.error("Storage error: %s: %s", type(error).__name__, error)
        raise HTTPException(
            status_code=502,
            detail="Storage operation failed",
        ) from error
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")

    try:
        report = service.get_mission_report(mission_id)
    except Exception as error:
        logger.error("Storage error: %s: %s", type(error).__name__, error)
        raise HTTPException(
            status_code=502,
            detail="Storage operation failed",
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


@router.post(
    "/missions/{mission_id}/force-complete",
    tags=["missions"],
    summary="Force-complete mission",
    response_model=ForceCompleteResponse,
    responses={
        404: {"description": "Mission not found"},
        409: {"description": "Could not resolve alerts or already completed"},
        502: {"description": "Storage operation failed"},
    },
)
def force_complete_mission(
    mission_id: str,
    payload: ForceCompleteRequest = ForceCompleteRequest(),
) -> dict[str, object]:
    """Emergency completion: automatically rejects all pending alerts
    and finalizes the mission.

    Use when the operator cannot review every alert individually."""
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
        logger.error("Storage error: %s: %s", type(error).__name__, error)
        raise HTTPException(
            status_code=502,
            detail="Storage operation failed",
        ) from error
    if completed is None:
        raise HTTPException(status_code=404, detail="Mission not found")

    try:
        report = service.get_mission_report(mission_id)
    except Exception as error:
        logger.error("Storage error: %s: %s", type(error).__name__, error)
        raise HTTPException(
            status_code=502,
            detail="Storage operation failed",
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


@router.get(
    "/missions/{mission_id}/stream/status",
    tags=["missions"],
    summary="Stream status",
    responses={404: {"description": "Mission not found"}},
)
def get_mission_stream_status(mission_id: str) -> dict[str, object]:
    """Return the current state of the video processing stream:
    running/stopped, processed frames, detection counters."""
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


@router.get(
    "/missions/{mission_id}/report",
    tags=["missions"],
    summary="Get mission report",
    responses={
        404: {"description": "Mission not found"},
        502: {"description": "Storage operation failed"},
    },
)
def get_mission_report(mission_id: str) -> dict[str, object]:
    """Return the quality report for a mission: detection statistics,
    alert counts, and KPI metrics (recall, false-positive rate, etc.)."""
    service = get_pilot_service()
    try:
        return service.get_mission_report(mission_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        logger.error("Storage error: %s: %s", type(error).__name__, error)
        raise HTTPException(
            status_code=502,
            detail="Storage operation failed",
        ) from error


# ── Alerts ─────────────────────────────────────────────────────────


@router.get(
    "/alerts",
    tags=["alerts"],
    summary="List alerts",
    response_model=list[AlertResponse],
    responses={404: {"description": "Mission not found (if filtered)"}},
)
def get_alerts(
    mission_id: str | None = None,
    status: str | None = None,
) -> list[dict[str, object]]:
    """List alerts with optional filters by mission and review status
    (queued, reviewed_confirmed, reviewed_rejected)."""
    service = get_pilot_service()
    if mission_id is not None and service.get_mission(mission_id) is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    alerts = service.list_alerts(mission_id=mission_id, status=status)
    return [_alert_to_dict(alert, service=service) for alert in alerts]


@router.get(
    "/alerts/{alert_id}",
    tags=["alerts"],
    summary="Get alert details",
    response_model=AlertResponse,
    responses={404: {"description": "Alert not found"}},
)
def get_alert_details(alert_id: str) -> dict[str, object]:
    """Return full details of a single alert: detection bounding boxes,
    confidence scores, review status, and timing information."""
    service = get_pilot_service()
    alert = service.get_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    return _alert_to_dict(alert, service=service)


@router.get(
    "/alerts/{alert_id}/frame",
    tags=["alerts"],
    summary="Download alert frame",
    responses={
        200: {"content": {"image/jpeg": {}}, "description": "Frame image"},
        404: {"description": "Alert or frame not found"},
        502: {"description": "Storage operation failed"},
    },
)
def get_alert_frame(alert_id: str) -> Response:
    """Download the original video frame (JPEG) that triggered the alert."""
    service = get_pilot_service()
    try:
        artifact = service.get_alert_frame_artifact(alert_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        logger.error("Storage error: %s: %s", type(error).__name__, error)
        raise HTTPException(
            status_code=502,
            detail="Storage operation failed",
        ) from error

    return Response(
        content=artifact.content,
        media_type=artifact.media_type,
        headers={"Content-Disposition": f'inline; filename="{artifact.filename}"'},
    )


@router.post(
    "/alerts/{alert_id}/confirm",
    tags=["alerts"],
    summary="Confirm alert",
    response_model=AlertResponse,
    responses={
        404: {"description": "Alert not found"},
        409: {"description": "Alert already reviewed"},
    },
)
def confirm_alert(
    alert_id: str, payload: ReviewRequest = ReviewRequest()
) -> dict[str, object]:
    """Mark the alert as a true positive — a person was indeed detected."""
    service = get_pilot_service()
    review_payload = cast(
        AlertReviewPayload,
        {
            "status": "reviewed_confirmed",
            "reviewed_by": payload.reviewed_by,
            "reviewed_at_sec": None,
            "decision_reason": None,
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


@router.post(
    "/alerts/{alert_id}/reject",
    tags=["alerts"],
    summary="Reject alert",
    response_model=AlertResponse,
    responses={
        404: {"description": "Alert not found"},
        409: {"description": "Alert already reviewed"},
    },
)
def reject_alert(
    alert_id: str, payload: ReviewRequest = ReviewRequest()
) -> dict[str, object]:
    """Mark the alert as a false positive — no real person in the frame."""
    service = get_pilot_service()
    review_payload = cast(
        AlertReviewPayload,
        {
            "status": "reviewed_rejected",
            "reviewed_by": payload.reviewed_by,
            "reviewed_at_sec": None,
            "decision_reason": None,
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


@router.post(
    "/predict",
    response_model=PredictResponse,
    tags=["detection"],
    summary="Single-frame detection",
    responses={
        404: {"description": "Frame not found in S3"},
        502: {"description": "Detection failed"},
        503: {"description": "Detector or storage not available"},
    },
)
def predict(payload: PredictRequest) -> PredictResponse:
    """Run YOLOv8 person detection on a single image.

    Accepts a local file path or an S3 URI. Returns a list of
    detected objects with bounding boxes and confidence scores."""
    logger.info("Predict request: image_uri=%s", payload.image_uri)
    detector = get_detector()
    if detector is None:
        raise HTTPException(
            status_code=503,
            detail="Detector not available (model not loaded)",
        )

    detect_source: object = payload.image_uri
    if payload.image_uri.startswith("s3://"):
        storage = get_artifact_storage()
        if storage is None:
            raise HTTPException(
                status_code=503,
                detail="Artifact storage not configured",
            )
        artifact = storage.load_frame(payload.image_uri)
        if artifact is None:
            logger.warning("Frame not found in S3: %s", payload.image_uri)
            raise HTTPException(
                status_code=404,
                detail="Frame not found",
            )
        detect_source = artifact.content
        logger.info(
            "Resolved S3 URI: %s (%d bytes)", payload.image_uri, len(artifact.content)
        )

    t0 = time.monotonic()
    try:
        detector_any: Any = detector
        detections: list[Detection] = detector_any.detect(detect_source)
    except Exception as error:
        logger.error(
            "Predict failed: image_uri=%s error=%s: %s",
            payload.image_uri,
            type(error).__name__,
            error,
        )
        raise HTTPException(status_code=502, detail="Detection failed") from error
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
        "has_frame": bool(alert.image_uri),
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
