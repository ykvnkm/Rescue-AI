"""FastAPI приложение для navigation engine (ADR-0008 §1).

Stateful сервис: держит ``NavigationEngine`` per-session в RAM,
возвращает ``session_id`` при reset, использует его при step.
Жизненный цикл сессии управляется клиентом (api):

    POST /sessions               → {session_id}
    POST /sessions/<id>/step     → {point | null}
    DELETE /sessions/<id>        → освободить память

Внутри — ровно тот же ``rescue_ai.navigation.engine.NavigationEngine``,
что используется монолитным API. Сервис не дублирует математику, он
её просто экспонирует по сети.
"""

from __future__ import annotations

import base64
import logging
import threading
import uuid
from dataclasses import dataclass

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Response

from rescue_ai.domain.value_objects import NavMode
from rescue_ai.navigation.engine import NavigationEngine
from rescue_ai.navigation.tuning import NavigationTuning
from rescue_ai.services.nav_engine.schemas import (
    ResetRequest,
    ResetResponse,
    StepRequest,
    StepResponse,
    TrajectoryPointResponse,
)

logger = logging.getLogger(__name__)


@dataclass
class _Session:
    engine: NavigationEngine
    mission_id: str


class _SessionRegistry:
    """Потокобезопасный реестр активных сессий.

    Используется обычный mutex — навигация stateful, параллельный
    ``step`` на одной сессии нелогичен. Запросы к разным сессиям не
    мешают друг другу.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    def create(self, mission_id: str, fps: float | None) -> str:
        session_id = uuid.uuid4().hex
        tuning = NavigationTuning() if fps is None else NavigationTuning(fps=fps)
        engine = NavigationEngine(mission_id=mission_id, config=tuning)
        with self._lock:
            self._sessions[session_id] = _Session(
                engine=engine, mission_id=mission_id
            )
        return session_id

    def get(self, session_id: str) -> _Session:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return session

    def drop(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None


def _decode_frame_jpeg(b64: str) -> np.ndarray:
    try:
        raw = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError) as err:
        raise HTTPException(
            status_code=400, detail=f"invalid base64: {err}"
        ) from err
    buffer = np.frombuffer(raw, dtype=np.uint8)
    frame = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if frame is None or frame.size == 0:
        raise HTTPException(
            status_code=400, detail="failed to decode JPEG frame"
        )
    return frame


def _coerce_nav_mode(value: str | None) -> NavMode | None:
    if value is None:
        return None
    try:
        return NavMode(value)
    except ValueError as err:
        raise HTTPException(
            status_code=400, detail=f"unknown nav_mode={value}"
        ) from err


def build_app(*, registry: _SessionRegistry | None = None) -> FastAPI:
    """Build a fresh FastAPI app + per-instance registry.

    Передача готового registry полезна тестам — проверяют без сетевого
    запуска. По умолчанию каждый процесс держит свой реестр.
    """

    app = FastAPI(title="rescue-ai-nav-engine", version="0.1.0")
    sessions = registry if registry is not None else _SessionRegistry()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/sessions", response_model=ResetResponse)
    def create_session(payload: ResetRequest) -> ResetResponse:
        session_id = sessions.create(
            mission_id=payload.mission_id, fps=payload.fps
        )
        nav_mode = _coerce_nav_mode(payload.nav_mode)
        sessions.get(session_id).engine.reset(
            nav_mode=nav_mode, fps=payload.fps
        )
        logger.info(
            "nav-engine session created: id=%s mission=%s nav_mode=%s fps=%s",
            session_id,
            payload.mission_id,
            payload.nav_mode,
            payload.fps,
        )
        return ResetResponse(session_id=session_id)

    @app.post("/sessions/{session_id}/step", response_model=StepResponse)
    def step(session_id: str, payload: StepRequest) -> StepResponse:
        session = sessions.get(session_id)
        frame = _decode_frame_jpeg(payload.frame_jpeg_b64)
        point = session.engine.step(
            frame_bgr=frame,
            ts_sec=payload.ts_sec,
            frame_id=payload.frame_id,
        )
        if point is None:
            return StepResponse(point=None)
        return StepResponse(
            point=TrajectoryPointResponse(
                mission_id=point.mission_id,
                seq=point.seq,
                ts_sec=point.ts_sec,
                x=point.x,
                y=point.y,
                z=point.z,
                source=str(point.source),
                frame_id=point.frame_id,
            )
        )

    @app.delete("/sessions/{session_id}")
    def drop_session(session_id: str) -> Response:
        sessions.drop(session_id)
        return Response(status_code=204)

    return app
