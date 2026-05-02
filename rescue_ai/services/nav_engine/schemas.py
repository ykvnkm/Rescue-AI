"""Wire schemas for the nav-engine HTTP service (ADR-0008 §1).

Кадры передаются как JPEG в base64 — формат компактный и одинаково
работает в Python/curl/browser. Если когда-нибудь упрёмся в latency,
переключимся на multipart/form-data с raw-байтами без изменения
доменной модели.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ResetRequest(BaseModel):
    """POST /sessions — создать новую сессию навигации.

    Сервис хранит engine instances per-session в RAM. ``mission_id``
    нужен только для логов / отладки, не для маршрутизации.
    """

    mission_id: str = Field(..., min_length=1, max_length=128)
    nav_mode: Literal["auto", "marker", "no_marker"] | None = None
    fps: float | None = Field(default=None, gt=0.0)


class ResetResponse(BaseModel):
    session_id: str


class StepRequest(BaseModel):
    """POST /sessions/{session_id}/step — обработать один кадр."""

    frame_jpeg_b64: str = Field(..., description="JPEG-кадр, base64-encoded")
    ts_sec: float
    frame_id: int | None = None


class TrajectoryPointResponse(BaseModel):
    """Соответствует ``rescue_ai.domain.entities.TrajectoryPoint``."""

    mission_id: str
    seq: int
    ts_sec: float
    x: float
    y: float
    z: float
    source: str
    frame_id: int | None = None


class StepResponse(BaseModel):
    point: TrajectoryPointResponse | None = None
