"""Automatic-mission API routes (ADR-0006)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from rescue_ai.domain.value_objects import NavMode
from rescue_ai.interfaces.api.dependencies import get_auto_mission_service
from rescue_ai.interfaces.api.logging_utils import sanitize_log_text

logger = logging.getLogger(__name__)
router = APIRouter()
DEFAULT_STREAM_FPS = 2.0


class AutoMissionStartRequest(BaseModel):
    """Request body for creating a new automatic mission."""

    source_name: str = Field(
        description="Logical source descriptor (e.g. rtsp URL or dataset name)",
    )
    nav_mode: NavMode = Field(
        default=NavMode.AUTO,
        description="Navigation mode: marker / no_marker / auto",
    )
    detector_name: str = Field(
        default="yolo",
        description="Detector identifier (yolo|nanodet)",
    )
    total_frames: int = Field(
        default=0,
        ge=0,
        description="Expected number of frames; 0 for open-ended streams",
    )
    fps: float = Field(
        default=DEFAULT_STREAM_FPS,
        gt=0.0,
        description="Frame rate used to compute ts_sec when the caller omits it",
    )
    config_json: dict[str, object] | None = Field(
        default=None,
        description="Free-form config snapshot persisted alongside the mission",
    )


class AutoMissionResponse(BaseModel):
    """Basic automatic mission descriptor."""

    mission_id: str = Field(description="Unique mission identifier")
    status: str = Field(description="Mission status", examples=["running"])
    mode: str = Field(description="Mission mode", examples=["automatic"])
    source_name: str = Field(description="Mission source")
    created_at: str = Field(description="UTC creation timestamp (ISO 8601)")
    fps: float = Field(description="Mission fps")


class AutoFrameDetectionItem(BaseModel):
    """One detection returned alongside a processed frame."""

    bbox: list[float]
    score: float
    label: str
    model_name: str


class AutoFramePoint(BaseModel):
    """Trajectory point produced for a frame, if any."""

    seq: int
    ts_sec: float
    frame_id: int | None
    x: float
    y: float
    z: float
    source: str


class AutoFrameAlertItem(BaseModel):
    """Alert emitted while ingesting a frame."""

    alert_id: str
    frame_id: int
    ts_sec: float
    people_detected: int
    score: float
    label: str
    image_uri: str


class AutoFrameDecisionItem(BaseModel):
    """Audit-log decision recorded for a frame."""

    decision_id: str
    kind: str
    reason: str
    ts_sec: float
    frame_id: int | None


class AutoFrameResponse(BaseModel):
    """Aggregated result of processing one automatic frame."""

    mission_id: str
    frame_id: int
    ts_sec: float
    detections: list[AutoFrameDetectionItem]
    trajectory_point: AutoFramePoint | None
    alerts: list[AutoFrameAlertItem]
    decisions: list[AutoFrameDecisionItem]


class AutoMissionCompleteResponse(BaseModel):
    """Response returned after completing an automatic mission."""

    mission_id: str
    status: str
    completed_frame_id: int | None
    report: dict[str, object]


def _require_auto_service() -> Any:
    service = get_auto_mission_service()
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Automatic mode not configured",
        )
    return service


def _auto_mission_to_response(mission: Any) -> dict[str, object]:
    return {
        "mission_id": mission.mission_id,
        "status": mission.status,
        "mode": str(mission.mode),
        "source_name": mission.source_name,
        "created_at": mission.created_at,
        "fps": mission.fps,
    }


def _auto_outcome_to_response(
    *,
    mission_id: str,
    frame_id: int,
    ts_sec: float,
    outcome: Any,
) -> dict[str, object]:
    trajectory_point = None
    if outcome.trajectory_point is not None:
        point = outcome.trajectory_point
        trajectory_point = {
            "seq": point.seq,
            "ts_sec": point.ts_sec,
            "frame_id": point.frame_id,
            "x": point.x,
            "y": point.y,
            "z": point.z,
            "source": str(point.source),
        }
    return {
        "mission_id": mission_id,
        "frame_id": frame_id,
        "ts_sec": ts_sec,
        "detections": [
            {
                "bbox": list(detection.bbox),
                "score": detection.score,
                "label": detection.label,
                "model_name": detection.model_name,
            }
            for detection in outcome.detections
        ],
        "trajectory_point": trajectory_point,
        "alerts": [
            {
                "alert_id": alert.alert_id,
                "frame_id": alert.frame_id,
                "ts_sec": alert.ts_sec,
                "people_detected": alert.people_detected,
                "score": alert.primary_detection.score,
                "label": alert.primary_detection.label,
                "image_uri": alert.image_uri,
            }
            for alert in outcome.alerts
        ],
        "decisions": [
            {
                "decision_id": decision.decision_id,
                "kind": str(decision.kind),
                "reason": decision.reason,
                "ts_sec": decision.ts_sec,
                "frame_id": decision.frame_id,
            }
            for decision in outcome.decisions
        ],
    }


@router.post(
    "/auto-missions/start",
    tags=["auto-missions"],
    summary="Start an automatic mission",
    response_model=AutoMissionResponse,
    responses={503: {"description": "Automatic mode not configured"}},
)
def start_auto_mission(payload: AutoMissionStartRequest) -> dict[str, object]:
    """Create and start a new automatic mission."""
    logger.info(
        "Endpoint start_auto_mission: source=%s nav_mode=%s detector=%s fps=%.2f",
        sanitize_log_text(payload.source_name),
        payload.nav_mode,
        payload.detector_name,
        payload.fps,
    )
    service = _require_auto_service()
    mission = service.start_auto_mission(
        source_name=payload.source_name,
        total_frames=payload.total_frames,
        fps=payload.fps,
        nav_mode=payload.nav_mode,
        detector_name=payload.detector_name,
        config_json=payload.config_json,
    )
    logger.info(
        "Endpoint start_auto_mission success: mission_id=%s", mission.mission_id
    )
    return _auto_mission_to_response(mission)


@router.post(
    "/auto-missions/{mission_id}/ingest",
    tags=["auto-missions"],
    summary="Ingest one frame",
    response_model=AutoFrameResponse,
    responses={
        404: {"description": "Mission not found"},
        409: {"description": "Mission is not an automatic mission or is completed"},
        415: {"description": "Uploaded file is not an image"},
        503: {"description": "Automatic mode not configured"},
    },
)
async def ingest_auto_mission_frame(
    mission_id: str,
    frame_id: int = Form(...),
    ts_sec: float = Form(...),
    image: UploadFile = File(...),
) -> dict[str, object]:
    """Process one frame of an automatic mission."""
    service = _require_auto_service()

    payload_bytes = await image.read()
    if not payload_bytes:
        raise HTTPException(status_code=415, detail="Empty frame payload")

    import numpy as np

    try:
        import cv2  # noqa: WPS433
    except ImportError as error:  # pragma: no cover
        raise HTTPException(
            status_code=503,
            detail="OpenCV is required to decode frame payloads",
        ) from error

    arr = np.frombuffer(payload_bytes, dtype=np.uint8)
    frame_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame_bgr is None:
        raise HTTPException(status_code=415, detail="Cannot decode frame payload")

    image_uri = f"upload://{mission_id}/{frame_id}/{image.filename or 'frame.jpg'}"
    try:
        outcome = service.ingest_frame(
            mission_id=mission_id,
            frame_bgr=frame_bgr,
            ts_sec=ts_sec,
            frame_id=frame_id,
            image_uri=image_uri,
        )
    except ValueError as error:
        text = str(error)
        if "Mission not found" in text:
            raise HTTPException(status_code=404, detail=text) from error
        raise HTTPException(status_code=409, detail=text) from error

    logger.info(
        "Endpoint ingest_auto_mission_frame success: mission_id=%s "
        "frame=%d detections=%d alerts=%d decisions=%d point=%s",
        mission_id,
        frame_id,
        len(outcome.detections),
        len(outcome.alerts),
        len(outcome.decisions),
        outcome.trajectory_point is not None,
    )
    return _auto_outcome_to_response(
        mission_id=mission_id,
        frame_id=frame_id,
        ts_sec=ts_sec,
        outcome=outcome,
    )


@router.post(
    "/auto-missions/{mission_id}/complete",
    tags=["auto-missions"],
    summary="Complete an automatic mission",
    response_model=AutoMissionCompleteResponse,
    responses={
        404: {"description": "Mission not found"},
        502: {"description": "Storage operation failed"},
        503: {"description": "Automatic mode not configured"},
    },
)
def complete_auto_mission(
    mission_id: str,
    completed_frame_id: int | None = None,
) -> dict[str, object]:
    """Finalize an automatic mission and persist the report + trajectory plot."""
    logger.info(
        "Endpoint complete_auto_mission: mission_id=%s completed_frame_id=%s",
        mission_id,
        completed_frame_id,
    )
    service = _require_auto_service()
    try:
        mission = service.complete_auto_mission(
            mission_id=mission_id, completed_frame_id=completed_frame_id
        )
    except Exception as error:
        logger.error("Storage error: %s", type(error).__name__)
        raise HTTPException(
            status_code=502, detail="Storage operation failed"
        ) from error
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")

    try:
        report = service.get_auto_mission_report(mission_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        logger.error("Storage error: %s", type(error).__name__)
        raise HTTPException(
            status_code=502, detail="Storage operation failed"
        ) from error

    logger.info(
        "Endpoint complete_auto_mission success: mission_id=%s status=%s",
        mission_id,
        mission.status,
    )
    return {
        "mission_id": mission.mission_id,
        "status": mission.status,
        "completed_frame_id": mission.completed_frame_id,
        "report": report,
    }


@router.get(
    "/auto-missions/{mission_id}/report",
    tags=["auto-missions"],
    summary="Get automatic-mission report",
    responses={
        404: {"description": "Mission not found or not completed"},
        502: {"description": "Storage operation failed"},
        503: {"description": "Automatic mode not configured"},
    },
)
def get_auto_mission_report(mission_id: str) -> dict[str, object]:
    """Return the ``report.json`` summary for a completed automatic mission."""
    logger.info("Endpoint get_auto_mission_report: mission_id=%s", mission_id)
    service = _require_auto_service()
    try:
        return dict(service.get_auto_mission_report(mission_id))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        logger.error("Storage error: %s", type(error).__name__)
        raise HTTPException(
            status_code=502, detail="Storage operation failed"
        ) from error


@router.get(
    "/auto-missions/{mission_id}/trajectory-plot.png",
    tags=["auto-missions"],
    summary="Download trajectory plot PNG",
    responses={
        404: {"description": "Mission or plot not found"},
        502: {"description": "Storage operation failed"},
        503: {"description": "Automatic mode not configured"},
    },
)
def get_auto_mission_trajectory_plot(mission_id: str) -> Response:
    """Serve the rendered ``plots/trajectory.png`` for an automatic mission."""
    logger.info("Endpoint get_auto_mission_trajectory_plot: mission_id=%s", mission_id)
    service = _require_auto_service()
    try:
        blob = service.load_trajectory_plot(mission_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        logger.error("Storage error: %s", type(error).__name__)
        raise HTTPException(
            status_code=502, detail="Storage operation failed"
        ) from error
    if blob is None:
        raise HTTPException(status_code=404, detail="Trajectory plot not available")
    return Response(
        content=blob.content,
        media_type=blob.media_type,
        headers={
            "Content-Disposition": f'inline; filename="{blob.filename}"',
        },
    )


@router.get(
    "/auto-missions/{mission_id}/trajectory.csv",
    tags=["auto-missions"],
    summary="Download trajectory CSV",
    responses={
        404: {"description": "Mission or trajectory CSV not found"},
        502: {"description": "Storage operation failed"},
        503: {"description": "Automatic mode not configured"},
    },
)
def get_auto_mission_trajectory_csv(mission_id: str) -> Response:
    """Serve the persisted ``trajectory.csv`` for an automatic mission."""
    logger.info("Endpoint get_auto_mission_trajectory_csv: mission_id=%s", mission_id)
    service = _require_auto_service()
    try:
        blob = service.load_trajectory_csv(mission_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        logger.error("Storage error: %s", type(error).__name__)
        raise HTTPException(
            status_code=502, detail="Storage operation failed"
        ) from error
    if blob is None:
        raise HTTPException(status_code=404, detail="Trajectory CSV not available")
    return Response(
        content=blob.content,
        media_type=blob.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{blob.filename}"',
        },
    )
