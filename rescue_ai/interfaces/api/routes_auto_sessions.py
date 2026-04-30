"""Automatic-mode session routes for the unified operator/auto UI.

Thin wrapper around :class:`AutoSessionManager`:

* ``POST /auto-sessions/start`` — upload a video (or name an RTSP url /
  frames folder) and begin streaming through the automatic pipeline.
* ``POST /auto-sessions/{session_id}/stop`` — request an early stop.
* ``GET  /auto-sessions/active`` — describe the currently running session.
* ``WS   /auto-sessions/{session_id}/stream`` — push per-frame snapshots
  (JPEG base64 + detections + trajectory point) to the browser.

Only one session runs at a time (NavigationEngine isn't thread-safe
across sessions). Uploads land in ``UploadSettings.uploads_dir``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel

from rescue_ai.application.auto_session_manager import (
    AutoSession,
    AutoSessionManager,
    StartSessionRequest,
)
from rescue_ai.config import get_settings
from rescue_ai.domain.value_objects import NavMode
from rescue_ai.interfaces.api.dependencies import get_auto_session_manager
from rescue_ai.interfaces.api.logging_utils import sanitize_log_text

logger = logging.getLogger(__name__)
router = APIRouter()

SourceKind = Literal["video", "rtsp", "frames"]


class AutoSessionStartResponse(BaseModel):
    """Response returned by POST /auto-sessions/start."""

    session_id: str
    mission_id: str
    source_kind: str
    source_value: str
    nav_mode: str
    detector_name: str
    fps: float
    started_at: str


class AutoSessionStopResponse(BaseModel):
    """Response returned by POST /auto-sessions/{id}/stop."""

    session_id: str
    mission_id: str
    frames_consumed: int
    frames_emitted: int
    alerts_total: int
    avg_stream_fps: float
    error: str | None = None


class AutoSessionActiveResponse(BaseModel):
    """Response returned by GET /auto-sessions/active."""

    running: bool
    session_id: str | None = None
    mission_id: str | None = None
    source_kind: str | None = None
    source_value: str | None = None
    nav_mode: str | None = None
    detector_name: str | None = None
    fps: float | None = None
    started_at: str | None = None
    frames_consumed: int = 0
    frames_emitted: int = 0
    alerts_total: int = 0
    avg_stream_fps: float = 0.0


def _as_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on"}


def _resolve_source_value(
    *,
    source_kind: SourceKind,
    source_value: str,
    file: UploadFile | None,
    nsu_channel: Literal["local", "stream"],
    rpi_mission_id: str,
) -> tuple[str, bool, str]:
    stream_channel = nsu_channel == "stream"
    mission_id_clean = rpi_mission_id.strip()
    if stream_channel:
        if source_kind == "rtsp":
            raise HTTPException(
                status_code=400,
                detail="RTSP source is not available in stream channel",
            )
        if not mission_id_clean:
            raise HTTPException(
                status_code=400,
                detail="rpi_mission_id is required when nsu_channel=stream",
            )
        return "", True, mission_id_clean

    resolved_value = source_value or ""
    if source_kind == "video" and file is not None:
        stored = _persist_upload(file)
        resolved_value = str(stored)
    if not resolved_value:
        raise HTTPException(
            status_code=400,
            detail=(
                "source_value (or uploaded file for source_kind=video) "
                "is required in local channel"
            ),
        )
    return resolved_value, False, mission_id_clean


def _require_manager() -> AutoSessionManager:
    manager = get_auto_session_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail="Automatic mode not configured")
    return manager


def _session_to_start_response(session: AutoSession) -> dict[str, object]:
    info = session.info()
    return {
        "session_id": info.session_id,
        "mission_id": info.mission_id,
        "source_kind": info.source_kind,
        "source_value": info.source_value,
        "nav_mode": info.nav_mode,
        "detector_name": info.detector_name,
        "fps": info.fps,
        "started_at": info.started_at,
    }


def _persist_upload(upload: UploadFile) -> Path:
    """Store the incoming file under ``UploadSettings.uploads_dir``.

    The filename is randomized to avoid collisions while preserving the
    original suffix (so ``cv2.VideoCapture`` can pick the right backend).
    Enforces ``UploadSettings.max_upload_mb``.
    """
    settings = get_settings()
    uploads_dir = Path(settings.uploads.uploads_dir)
    uploads_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(upload.filename or "").suffix.lower() or ".mp4"
    target = uploads_dir / f"{uuid.uuid4().hex}{suffix}"

    max_bytes = settings.uploads.max_upload_mb * 1024 * 1024
    written = 0
    chunk_size = 1024 * 1024
    with target.open("wb") as fh:
        while True:
            chunk = upload.file.read(chunk_size)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                fh.close()
                target.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"upload exceeds UPLOAD_MAX_MB="
                        f"{settings.uploads.max_upload_mb}MB"
                    ),
                )
            fh.write(chunk)
    return target


@router.post(
    "/auto-sessions/start",
    tags=["auto-sessions"],
    summary="Start a new automatic-mode session",
    response_model=AutoSessionStartResponse,
    responses={
        400: {"description": "Invalid source"},
        409: {"description": "Another automatic session is already running"},
        413: {"description": "Upload too large"},
        503: {"description": "Automatic mode not configured"},
    },
)
async def start_auto_session(
    request: Request,
) -> dict[str, object]:
    """Create an automatic mission and start streaming frames."""
    manager = _require_manager()

    form = await request.form()
    source_kind = cast(SourceKind, str(form.get("source_kind", "video")))
    source_value = str(form.get("source_value", "") or "")
    nav_mode = NavMode(str(form.get("nav_mode", NavMode.AUTO)))
    detector_name = str(form.get("detector_name", "yolo"))
    fps_raw = form.get("fps", 6.0)
    if isinstance(fps_raw, (str, int, float)):
        fps = float(fps_raw)
    else:
        fps = 6.0
    nsu_channel = cast(
        Literal["local", "stream"], str(form.get("nsu_channel", "local"))
    )
    rpi_mission_id = str(form.get("rpi_mission_id", ""))

    detect_enabled = _as_bool(form.get("detect_enabled"), True)
    save_video = _as_bool(form.get("save_video"), False)
    demo_loop = _as_bool(form.get("demo_loop"), False)
    file_obj = form.get("file")
    file = file_obj if isinstance(file_obj, UploadFile) else None
    resolved_value, stream_channel, mission_id_clean = _resolve_source_value(
        source_kind=source_kind,
        source_value=source_value,
        file=file,
        nsu_channel=nsu_channel,
        rpi_mission_id=rpi_mission_id,
    )

    try:
        source, canonical_value = manager.build_source(
            source_kind=source_kind,
            source_value=resolved_value,
            fps=fps,
            rpi_mission_id=mission_id_clean if stream_channel else "",
            demo_loop=bool(demo_loop and not stream_channel and source_kind == "video"),
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    logger.info(
        "Endpoint start_auto_session: channel=%s kind=%s value=%s nav=%s "
        "detector=%s fps=%.2f detect=%s save_video=%s demo_loop=%s",
        nsu_channel,
        source_kind,
        sanitize_log_text(canonical_value),
        nav_mode,
        detector_name,
        fps,
        detect_enabled,
        save_video,
        demo_loop,
    )
    config_json: dict[str, object] = {
        "nsu_channel": nsu_channel,
        "detect_enabled": bool(detect_enabled),
        "save_video": bool(save_video),
        "demo_loop": bool(demo_loop),
    }
    if stream_channel:
        config_json["rpi_mission_id"] = mission_id_clean
    try:
        session = manager.start_session(
            request=StartSessionRequest(
                source=source,
                source_kind=source_kind,
                source_value=canonical_value,
                nav_mode=nav_mode,
                detector_name=detector_name,
                fps=fps,
                config_json=config_json,
                detect_enabled=bool(detect_enabled),
                save_video=bool(save_video),
            ),
        )
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    logger.info(
        "Endpoint start_auto_session success: session_id=%s mission_id=%s",
        session.session_id,
        session.mission.mission_id,
    )
    return _session_to_start_response(session)


@router.post(
    "/auto-sessions/{session_id}/stop",
    tags=["auto-sessions"],
    summary="Stop an automatic-mode session",
    response_model=AutoSessionStopResponse,
    responses={
        404: {"description": "Session not found"},
        503: {"description": "Automatic mode not configured"},
    },
)
def stop_auto_session(session_id: str) -> dict[str, object]:
    """Signal the session to stop and wait for it to drain."""
    manager = _require_manager()
    try:
        session = manager.stop_session(session_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    stats = session.stats()
    logger.info(
        "Endpoint stop_auto_session: session_id=%s frames=%d alerts=%d",
        session_id,
        stats.frames_consumed,
        stats.alerts_total,
    )
    return {
        "session_id": session.session_id,
        "mission_id": session.mission.mission_id,
        "frames_consumed": stats.frames_consumed,
        "frames_emitted": stats.frames_emitted,
        "alerts_total": stats.alerts_total,
        "avg_stream_fps": stats.avg_stream_fps,
        "error": stats.last_error,
    }


@router.get(
    "/auto-sessions/active",
    tags=["auto-sessions"],
    summary="Describe the currently active automatic session (if any)",
    response_model=AutoSessionActiveResponse,
    responses={503: {"description": "Automatic mode not configured"}},
)
def get_active_auto_session() -> dict[str, object]:
    """Return descriptor + running counters for the active session."""
    manager = _require_manager()
    session = manager.get_active()
    if session is None:
        return {"running": False}
    info = session.info()
    stats = session.stats()
    return {
        "running": session.is_alive,
        "session_id": info.session_id,
        "mission_id": info.mission_id,
        "source_kind": info.source_kind,
        "source_value": info.source_value,
        "nav_mode": info.nav_mode,
        "detector_name": info.detector_name,
        "fps": info.fps,
        "started_at": info.started_at,
        "frames_consumed": stats.frames_consumed,
        "frames_emitted": stats.frames_emitted,
        "alerts_total": stats.alerts_total,
        "avg_stream_fps": stats.avg_stream_fps,
    }


@router.websocket("/auto-sessions/{session_id}/stream")
async def auto_session_stream(websocket: WebSocket, session_id: str) -> None:
    """Forward every session event to one WebSocket client."""
    manager = get_auto_session_manager()
    if manager is None:
        await websocket.close(code=1011, reason="automatic mode not configured")
        return

    try:
        session = manager.require(session_id)
    except LookupError:
        await websocket.close(code=1008, reason="session not found")
        return

    await websocket.accept()

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=256)

    def _enqueue(event: Any) -> None:
        """Subscriber runs on session thread; bridge via thread-safe enqueue."""
        try:
            asyncio.run_coroutine_threadsafe(queue.put(event), loop)
        except RuntimeError:  # pragma: no cover - loop may be closing
            logger.debug("auto-session WS: enqueue after loop stopped", exc_info=True)

    session.subscribe(_enqueue)

    try:
        # If the session already completed before the client connected,
        # send a terminal event and close.
        if not session.is_alive:
            await websocket.send_json(
                {
                    "type": "done",
                    "session_id": session.session_id,
                    "mission_id": session.mission.mission_id,
                    "note": "session already completed",
                }
            )
            await websocket.close()
            return

        while True:
            event = await queue.get()
            await websocket.send_json(event)
            event_type = event.get("type") if isinstance(event, dict) else None
            if event_type in {"done", "error"}:
                # Drain any remaining queued events, then close.
                while not queue.empty():
                    pending = queue.get_nowait()
                    await websocket.send_json(pending)
                break
    except WebSocketDisconnect:
        logger.info("auto-session WS disconnected: session_id=%s", session_id)
    except (RuntimeError, ValueError, TypeError):  # pragma: no cover
        logger.exception("auto-session WS error: session_id=%s", session_id)
    finally:
        session.unsubscribe(_enqueue)
        try:
            await websocket.close()
        except RuntimeError:  # pragma: no cover - already closed
            pass
