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
import hashlib
import json
import logging
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from starlette.datastructures import UploadFile

from rescue_ai.application.auto_session_manager import (
    AutoSession,
    AutoSessionManager,
    StartSessionRequest,
)
from rescue_ai.config import get_settings
from rescue_ai.domain.geo import is_valid_lat, is_valid_lon
from rescue_ai.domain.value_objects import NavMode
from rescue_ai.interfaces.api.dependencies import get_auto_session_manager
from rescue_ai.interfaces.api.logging_utils import sanitize_log_text

logger = logging.getLogger(__name__)
router = APIRouter()

SourceKind = Literal["video", "rtsp", "frames", "s3"]


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


def _parse_source_kind(raw_value: object) -> SourceKind:
    value = str(raw_value or "").strip()
    if value == "video":
        return "video"
    if value == "rtsp":
        return "rtsp"
    if value == "frames":
        return "frames"
    if value == "s3":
        return "s3"
    raise HTTPException(
        status_code=400,
        detail="source_kind must be one of: video, rtsp, frames, s3",
    )


def _parse_positive_fps(raw_value: object) -> float:
    if not isinstance(raw_value, (str, int, float)):
        raise HTTPException(status_code=400, detail="fps must be a positive float")
    try:
        fps = float(raw_value)
    except (TypeError, ValueError) as error:
        raise HTTPException(
            status_code=400,
            detail="fps must be a positive float",
        ) from error
    if fps <= 0.0:
        raise HTTPException(status_code=400, detail="fps must be a positive float")
    return fps


def _parse_origin(raw_lat: object, raw_lon: object) -> tuple[float, float] | None:
    """Parse an optional absolute start point from the form.

    Returns ``(lat, lon)`` when both are present and valid, ``None`` when both
    are absent. Raises 400 on a partial/invalid pair.
    """
    lat_str = str(raw_lat or "").strip()
    lon_str = str(raw_lon or "").strip()
    if not lat_str and not lon_str:
        return None
    if not lat_str or not lon_str:
        raise HTTPException(
            status_code=400,
            detail="origin requires both origin_lat and origin_lon",
        )
    try:
        lat, lon = float(lat_str), float(lon_str)
    except ValueError as error:
        raise HTTPException(
            status_code=400, detail="origin_lat/origin_lon must be numbers"
        ) from error
    if not is_valid_lat(lat) or not is_valid_lon(lon):
        raise HTTPException(
            status_code=400,
            detail="origin out of range (lat ∈ [-90,90], lon ∈ [-180,180])",
        )
    return lat, lon


def _parse_channel(raw_value: object) -> Literal["local", "stream"]:
    value = str(raw_value or "local")
    if value == "local":
        return "local"
    if value == "stream":
        return "stream"
    raise HTTPException(status_code=400, detail="nsu_channel must be local or stream")


def _resolve_source_value(
    *,
    source_kind: SourceKind,
    source_value: str,
    file: UploadFile | None,
    nsu_channel: Literal["local", "stream"],
    rpi_mission_id: str,
) -> tuple[str, bool, str, str]:
    """Resolve the concrete cv2 source value and a stable mission identity.

    Returns ``(resolved_value, stream_channel, rpi_mission_id, mission_key)``.
    ``mission_key`` is content-derived for uploads (sha256) so re-feeding the
    same file reuses the same mission; for path/url/stream sources it is the
    (already stable) ``kind:value`` / ``rpi:id`` key.
    """
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
        return "", True, mission_id_clean, f"rpi:{mission_id_clean}"

    resolved_value = source_value or ""
    mission_key = ""
    if source_kind == "video" and file is not None:
        stored, digest = _persist_upload(file)
        resolved_value = str(stored)
        original = Path(file.filename or "").name or "video"
        mission_key = f"upload:{original}:{digest[:12]}"
    elif source_kind == "frames" and file is not None:
        # Offline profile: a ZIP mission package is uploaded (frames/ +
        # optional labels.json, mirroring the S3 layout). Extract to a
        # content-addressable workspace and replay the frames folder; the
        # workspace dir name is the archive sha256.
        workspace = _persist_upload_zip(file)
        resolved_value = str(workspace / "frames")
        original = Path(file.filename or "").name or "frames.zip"
        mission_key = f"frames:{original}:{workspace.name[:12]}"
    if not resolved_value:
        raise HTTPException(
            status_code=400,
            detail=(
                "source_value (or uploaded file for source_kind=video) "
                "is required in local channel"
            ),
        )
    if not mission_key:
        mission_key = f"{source_kind}:{resolved_value}"
    return resolved_value, False, mission_id_clean, mission_key


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


def _persist_upload(upload: UploadFile) -> tuple[Path, str]:
    """Store the incoming file content-addressably under ``uploads_dir``.

    Streams the upload into a staging file while computing its sha256, then
    renames it to ``<sha256><suffix>``. Re-uploading identical content reuses
    the existing file (and yields the same mission identity). The suffix is
    preserved so ``cv2.VideoCapture`` picks the right backend. Enforces
    ``UploadSettings.max_upload_mb``. Returns ``(path, sha256_hex)``.
    """
    settings = get_settings()
    uploads_dir = Path(settings.uploads.uploads_dir)
    uploads_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(upload.filename or "").suffix.lower() or ".mp4"
    max_bytes = settings.uploads.max_upload_mb * 1024 * 1024

    hasher = hashlib.sha256()
    staging = uploads_dir / f".incoming-{uuid.uuid4().hex}{suffix}"
    written = 0
    chunk_size = 1024 * 1024
    try:
        with staging.open("wb") as fh:
            while True:
                chunk = upload.file.read(chunk_size)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"upload exceeds UPLOAD_MAX_MB="
                            f"{settings.uploads.max_upload_mb}MB"
                        ),
                    )
                hasher.update(chunk)
                fh.write(chunk)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise

    digest = hasher.hexdigest()
    target = uploads_dir / f"{digest}{suffix}"
    if target.exists():
        staging.unlink(missing_ok=True)
    else:
        staging.replace(target)
    return target, digest


_FRAME_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def _persist_upload_zip(upload: UploadFile) -> Path:
    """Stream a ZIP mission package and extract it content-addressably.

    The archive mirrors the canonical S3 mission layout — ``frames/<img>``
    plus an optional ``labels.json`` — so an offline upload lands in S3 in
    exactly the same shape as a cloud mission. The archive is hashed while
    streamed and extracted into ``uploads_dir/missions/<sha>/`` (frames into
    ``frames/`` flattened/zip-slip-guarded, ``labels.json`` at the root).
    Flat archives (images at the root, no ``frames/``) are also accepted and
    treated as frames. Re-uploading the same archive reuses the workspace.
    Returns the mission workspace directory. ``UploadSettings.max_upload_mb``
    is enforced.
    """
    settings = get_settings()
    uploads_dir = Path(settings.uploads.uploads_dir)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = settings.uploads.max_upload_mb * 1024 * 1024

    hasher = hashlib.sha256()
    staging = uploads_dir / f".incoming-{uuid.uuid4().hex}.zip"
    written = 0
    chunk_size = 1024 * 1024
    try:
        with staging.open("wb") as fh:
            while True:
                chunk = upload.file.read(chunk_size)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"upload exceeds UPLOAD_MAX_MB="
                            f"{settings.uploads.max_upload_mb}MB"
                        ),
                    )
                hasher.update(chunk)
                fh.write(chunk)

        digest = hasher.hexdigest()
        workspace = uploads_dir / "missions" / digest
        frames_dir = workspace / "frames"
        if frames_dir.is_dir() and any(frames_dir.iterdir()):
            return workspace  # identical archive already extracted

        frames_dir.mkdir(parents=True, exist_ok=True)
        _extract_mission_zip(staging, workspace)
        if not any(frames_dir.iterdir()):
            raise HTTPException(
                status_code=400,
                detail="zip archive contains no frame images (jpg/png/bmp/webp)",
            )
        return workspace
    except zipfile.BadZipFile as error:
        raise HTTPException(status_code=400, detail="invalid zip archive") from error
    finally:
        staging.unlink(missing_ok=True)


def _read_zip_labels(workspace: Path) -> dict[str, object] | None:
    """Return the parsed ``labels.json`` from a ZIP workspace, or None."""
    labels_file = workspace / "labels.json"
    if not labels_file.is_file():
        return None
    try:
        payload = json.loads(labels_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HTTPException(
            status_code=400, detail="labels.json in archive is not valid JSON"
        ) from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="labels.json must be a JSON object")
    return payload


def _extract_mission_zip(zip_path: Path, workspace: Path) -> None:
    """Extract a mission package: images → ``frames/``, ``labels.json`` → root.

    Member names are flattened to their basename (guards against zip-slip).
    Both ``frames/x.jpg`` and a flat ``x.jpg`` map to ``frames/x.jpg``.
    """
    frames_dir = workspace / "frames"
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name
            if not name:
                continue
            suffix = Path(name).suffix.lower()
            if suffix in _FRAME_SUFFIXES:
                target = frames_dir / name
            elif name == "labels.json":
                target = workspace / "labels.json"
            else:
                continue
            with archive.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


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

    source_kind = _parse_source_kind(form.get("source_kind"))
    source_value = str(form.get("source_value", "") or "")
    nav_mode = NavMode(str(form.get("nav_mode", NavMode.AUTO.value)))
    detector_name = str(form.get("detector_name", "yolo"))
    fps = _parse_positive_fps(form.get("fps", 6.0))
    nsu_channel = _parse_channel(form.get("nsu_channel", "local"))
    rpi_mission_id = str(form.get("rpi_mission_id", "") or "")
    detect_enabled = _as_bool(form.get("detect_enabled"), True)
    save_video = _as_bool(form.get("save_video"), False)
    demo_loop = _as_bool(form.get("demo_loop"), False)
    form_file = form.get("file")
    file: UploadFile | None = form_file if isinstance(form_file, UploadFile) else None

    resolved_value, stream_channel, mission_id_clean, mission_key = (
        _resolve_source_value(
            source_kind=source_kind,
            source_value=source_value,
            file=file,
            nsu_channel=nsu_channel,
            rpi_mission_id=rpi_mission_id,
        )
    )

    # An uploaded ZIP may carry a labels.json next to frames/ (same shape as
    # an S3 mission); ship it to S3 so the archived mission stays symmetric.
    labels_payload: dict[str, object] | None = None
    if source_kind == "frames" and file is not None:
        labels_payload = _read_zip_labels(Path(resolved_value).parent)

    # The operator FPS is the target *processing* rate for every source. For a
    # local video file the factory downsamples the file to this rate (native
    # timestamps preserved) instead of running inference on every frame of a
    # high-FPS clip; streams use it directly.
    factory_fps: float | None = fps

    try:
        source, canonical_value, source_fps = manager.build_source(
            source_kind=source_kind,
            source_value=resolved_value,
            fps=factory_fps,
            rpi_mission_id=mission_id_clean if stream_channel else "",
            demo_loop=bool(demo_loop and not stream_channel and source_kind == "video"),
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    effective_fps = float(source_fps)
    logger.info(
        "Endpoint start_auto_session: channel=%s kind=%s value=%s nav=%s "
        "detector=%s fps=%.2f detect=%s save_video=%s demo_loop=%s",
        nsu_channel,
        source_kind,
        sanitize_log_text(canonical_value),
        nav_mode,
        detector_name,
        effective_fps,
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
    # Optional absolute start point. When provided, the trajectory is reported
    # in geographic coordinates instead of meters-relative-to-origin.
    origin = _parse_origin(form.get("origin_lat"), form.get("origin_lon"))
    if origin is not None:
        config_json["origin_lat"], config_json["origin_lon"] = origin
    # Re-running a mission from S3 pins its existing id so the replay
    # overwrites that mission in place rather than minting a new one.
    mission_id_override: str | None = None
    if source_kind == "s3":
        mission_id_override = resolved_value.partition("/")[2] or None
    try:
        session = manager.start_session(
            request=StartSessionRequest(
                source=source,
                source_kind=source_kind,
                source_value=canonical_value,
                nav_mode=nav_mode,
                detector_name=detector_name,
                fps=effective_fps,
                # Operator's chosen FPS throttles ONLY the detector; navigation
                # always runs at the source's real (effective) frame rate. For a
                # video file effective_fps is the native rate, so detection runs
                # every round(native/detect_fps)-th frame while nav sees them all.
                detect_fps=float(fps),
                config_json=config_json,
                detect_enabled=bool(detect_enabled),
                save_video=bool(save_video),
                mission_key=mission_key,
                mission_id=mission_id_override,
                labels_payload=labels_payload,
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


@router.get(
    "/s3-missions",
    tags=["auto-sessions"],
    summary="List missions archived in S3 (re-runnable from the cloud)",
)
def list_cloud_missions() -> dict[str, object]:
    """Return ``{ds, mission_id}`` entries for every mission stored in S3.

    Powers the operator UI "Из облака" picker. Returns an empty list (not an
    error) when S3 is not configured so the UI degrades gracefully.
    """
    from rescue_ai.infrastructure.artifact_storage import (  # noqa: PLC0415
        S3ArtifactBackendSettings,
    )
    from rescue_ai.infrastructure.s3_mission_source import (  # noqa: PLC0415
        list_s3_missions,
    )

    storage = get_settings().storage
    if not storage.s3_bucket or not storage.s3_access_key_id:
        return {"missions": []}

    s3_settings = S3ArtifactBackendSettings(
        endpoint=storage.s3_endpoint,
        region=storage.s3_region,
        access_key_id=storage.s3_access_key_id,
        secret_access_key=storage.s3_secret_access_key,
        bucket=storage.s3_bucket,
        prefix=storage.s3_prefix,
    )
    try:
        missions = list_s3_missions(s3_settings, source_prefix=storage.s3_prefix or "")
    except (OSError, RuntimeError, ValueError) as error:
        logger.warning("list_cloud_missions failed: %s", error)
        raise HTTPException(status_code=502, detail="S3 listing failed") from error
    return {"missions": missions}


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
