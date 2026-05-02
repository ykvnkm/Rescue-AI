"""HTTP-адаптер для ``NavigationEnginePort`` (ADR-0008 §1).

Делает HTTP-вызовы в ``rescue-ai-nav-engine`` и реализует тот же
порт, что и локальный :class:`NavigationEngine`. Вызывающий код в
``AutoMissionService`` его не отличает.

Реестр сессий — на стороне сервера. Адаптер хранит у себя только
текущий ``session_id`` (создаётся при ``reset``, удаляется при
следующем ``reset`` или ``close``).
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import cv2
import httpx
import numpy as np

from rescue_ai.domain.entities import TrajectoryPoint
from rescue_ai.domain.value_objects import NavMode, TrajectorySource

logger = logging.getLogger(__name__)


class HttpNavigationEngine:
    """``NavigationEnginePort`` поверх HTTP."""

    def __init__(
        self,
        base_url: str,
        mission_id: str,
        *,
        timeout_sec: float = 5.0,
        client: httpx.Client | None = None,
        jpeg_quality: int = 85,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._mission_id = mission_id
        self._timeout = timeout_sec
        self._jpeg_quality = int(max(50, min(95, jpeg_quality)))
        # ``client`` инжектируется в тестах. Реальный wiring создаёт
        # один общий httpx.Client на сервис, чтобы переиспользовать
        # keep-alive соединения.
        self._client = client or httpx.Client(timeout=timeout_sec)
        self._session_id: str | None = None

    # ── NavigationEnginePort ────────────────────────────────────

    def reset(
        self,
        *,
        nav_mode: NavMode | None = None,
        fps: float | None = None,
    ) -> None:
        # Новый reset = новая сессия. Старую отпускаем best-effort.
        if self._session_id is not None:
            self._drop_session(self._session_id)
            self._session_id = None
        payload: dict[str, Any] = {"mission_id": self._mission_id}
        if nav_mode is not None and nav_mode != NavMode.AUTO:
            payload["nav_mode"] = str(nav_mode)
        if fps is not None and fps > 0.0:
            payload["fps"] = float(fps)
        response = self._client.post(
            f"{self._base_url}/sessions",
            json=payload,
            timeout=self._timeout,
        )
        response.raise_for_status()
        self._session_id = str(response.json()["session_id"])
        logger.info(
            "HttpNavigationEngine: session created id=%s mission=%s",
            self._session_id,
            self._mission_id,
        )

    def step(
        self,
        frame_bgr: object,
        ts_sec: float,
        frame_id: int | None = None,
    ) -> TrajectoryPoint | None:
        if self._session_id is None:
            raise RuntimeError(
                "HttpNavigationEngine.step called before reset()"
            )
        b64 = _encode_jpeg_b64(frame_bgr, self._jpeg_quality)
        response = self._client.post(
            f"{self._base_url}/sessions/{self._session_id}/step",
            json={
                "frame_jpeg_b64": b64,
                "ts_sec": float(ts_sec),
                "frame_id": frame_id,
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        body = response.json()
        point_payload = body.get("point")
        if point_payload is None:
            return None
        return _trajectory_point_from_payload(point_payload)

    # ── Lifecycle ────────────────────────────────────────────────

    def close(self) -> None:
        if self._session_id is not None:
            self._drop_session(self._session_id)
            self._session_id = None
        self._client.close()

    def _drop_session(self, session_id: str) -> None:
        try:
            self._client.delete(
                f"{self._base_url}/sessions/{session_id}",
                timeout=self._timeout,
            )
        except httpx.HTTPError as err:  # pragma: no cover — best-effort
            logger.debug("nav-engine session %s drop failed: %s", session_id, err)


def _encode_jpeg_b64(frame_bgr: object, quality: int) -> str:
    frame = np.asarray(frame_bgr)
    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError(
            f"frame must be HxWx3 BGR, got shape={getattr(frame, 'shape', None)}"
        )
    success, buffer = cv2.imencode(
        ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
    )
    if not success:
        raise RuntimeError("cv2.imencode failed for navigation frame")
    return base64.b64encode(buffer.tobytes()).decode("ascii")


def _trajectory_point_from_payload(payload: dict[str, Any]) -> TrajectoryPoint:
    source_raw = str(payload.get("source", "marker"))
    try:
        source = TrajectorySource(source_raw)
    except ValueError:
        source = TrajectorySource.MARKER
    return TrajectoryPoint(
        mission_id=str(payload["mission_id"]),
        seq=int(payload["seq"]),
        ts_sec=float(payload["ts_sec"]),
        x=float(payload["x"]),
        y=float(payload["y"]),
        z=float(payload["z"]),
        source=source,
        frame_id=(
            None
            if payload.get("frame_id") is None
            else int(payload["frame_id"])
        ),
    )


__all__ = ["HttpNavigationEngine"]
