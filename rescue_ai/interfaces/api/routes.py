"""FastAPI route handlers for Rescue-AI API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from rescue_ai.config import get_settings
from rescue_ai.interfaces.api.dependencies import (
    get_pilot_service,
    get_stream_controller,
)
from rescue_ai.interfaces.api.ui_page import build_ui_html

router = APIRouter()
DEFAULT_STREAM_FPS = 6.0


class HealthResponse(BaseModel):
    status: str = Field(description="Service health status", examples=["ok"])


class ReadyResponse(BaseModel):
    status: str = Field(description="Readiness status", examples=["ready"])
    checks: dict[str, bool] = Field(
        description="Configuration checks for required external integrations"
    )


class RpiStatusResponse(BaseModel):
    connected: bool = Field(description="Whether RPi service is reachable")
    base_url: str = Field(description="RPi service base URL")
    detail: str | None = Field(default=None, description="Failure details")


class RpiMissionItemResponse(BaseModel):
    mission_id: str = Field(description="Mission ID on Raspberry Pi")
    name: str = Field(description="Human-readable mission name")


class RpiMissionsResponse(BaseModel):
    missions: list[RpiMissionItemResponse]


class PredictStartRequest(BaseModel):
    rpi_mission_id: str = Field(description="Mission ID on Raspberry Pi")


class PredictStartResponse(BaseModel):
    mission_id: str
    status: str
    source_name: str
    fps: float
    stream: dict[str, object]


class PredictStatusResponse(BaseModel):
    mission_id: str
    status: str
    completed_frame_id: int | None
    stream: dict[str, object] | None


class PredictStopResponse(BaseModel):
    mission_id: str
    status: str
    stream: dict[str, object] | None
    report: dict[str, object]


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/ready", response_model=ReadyResponse, tags=["system"])
def ready() -> ReadyResponse:
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
    return ReadyResponse(status="ready", checks=checks)


@router.get("/rpi/status", response_model=RpiStatusResponse, tags=["system"])
def rpi_status() -> RpiStatusResponse:
    settings = get_settings()
    base_url = settings.rpi.base_url
    if not base_url:
        return RpiStatusResponse(
            connected=False, base_url="", detail="RPI_BASE_URL not configured"
        )

    stream_controller = get_stream_controller()
    try:
        stream_controller.check_rpi_health()
        return RpiStatusResponse(connected=True, base_url=base_url)
    except (ValueError, RuntimeError, OSError) as error:
        return RpiStatusResponse(
            connected=False,
            base_url=base_url,
            detail=f"{type(error).__name__}: {error}",
        )


@router.get("/rpi/missions", response_model=RpiMissionsResponse, tags=["system"])
def rpi_missions() -> RpiMissionsResponse:
    stream_controller = get_stream_controller()
    try:
        missions = stream_controller.list_rpi_missions()
    except (ValueError, RuntimeError, OSError) as error:
        raise HTTPException(
            status_code=503,
            detail=f"RPi catalog fetch failed: {type(error).__name__}: {error}",
        ) from error
    return RpiMissionsResponse(
        missions=[
            RpiMissionItemResponse(
                mission_id=item.get("mission_id", ""),
                name=item.get("name", item.get("mission_id", "")),
            )
            for item in missions
            if item.get("mission_id", "")
        ]
    )


@router.get("/pilot", include_in_schema=False, response_class=HTMLResponse)
def pilot_ui() -> HTMLResponse:
    return HTMLResponse(content=build_ui_html())


@router.post("/predict/start", response_model=PredictStartResponse, tags=["predict"])
def start_predict(payload: PredictStartRequest) -> PredictStartResponse:
    service = get_pilot_service()
    stream_controller = get_stream_controller()

    mission = service.create_mission(
        source_name=f"rpi:{payload.rpi_mission_id}",
        total_frames=0,
        fps=DEFAULT_STREAM_FPS,
    )
    started = service.start_mission(mission.mission_id)
    if started is None:
        raise HTTPException(status_code=404, detail="Mission not found")

    try:
        stream = stream_controller.start(
            mission_id=mission.mission_id,
            rpi_mission_id=payload.rpi_mission_id,
            target_fps=DEFAULT_STREAM_FPS,
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

    return PredictStartResponse(
        mission_id=mission.mission_id,
        status=started.status,
        source_name=mission.source_name,
        fps=mission.fps,
        stream=stream_controller.as_payload(mission.mission_id)
        or {"session_id": getattr(stream, "session_id", "")},
    )


@router.get(
    "/predict/{mission_id}", response_model=PredictStatusResponse, tags=["predict"]
)
def get_predict_status(mission_id: str) -> PredictStatusResponse:
    service = get_pilot_service()
    stream_controller = get_stream_controller()

    mission = service.get_mission(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")

    return PredictStatusResponse(
        mission_id=mission.mission_id,
        status=mission.status,
        completed_frame_id=mission.completed_frame_id,
        stream=stream_controller.as_payload(mission_id),
    )


@router.post(
    "/predict/{mission_id}/stop", response_model=PredictStopResponse, tags=["predict"]
)
def stop_predict(mission_id: str) -> PredictStopResponse:
    service = get_pilot_service()
    stream_controller = get_stream_controller()

    mission = service.get_mission(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")

    stream_controller.stop(mission_id)

    updated = service.complete_mission(mission_id=mission_id, completed_frame_id=None)
    if updated is None:
        raise HTTPException(status_code=404, detail="Mission not found")

    try:
        report = service.get_mission_report(mission_id)
    except (ValueError, RuntimeError, OSError) as error:
        raise HTTPException(
            status_code=502,
            detail=f"Report generation error: {type(error).__name__}: {error}",
        ) from error

    return PredictStopResponse(
        mission_id=updated.mission_id,
        status=updated.status,
        stream=stream_controller.as_payload(mission_id),
        report=report,
    )
