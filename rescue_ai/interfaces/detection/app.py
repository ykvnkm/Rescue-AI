"""FastAPI приложение для детектора (ADR-0008 §1).

Stateless: один глобальный ``DetectorPort`` instance на процесс,
запросы независимы друг от друга. ``warmup`` вызывается на старте
приложения через FastAPI lifespan, чтобы первый запрос не платил за
загрузку модели.

API сервиса:

    GET  /health              → {"status": "ok"}
    GET  /ready               → {"status": "ready"}
    GET  /runtime             → {"runtime_name": "..."}
    POST /detect              → list[Detection]
"""

from __future__ import annotations

import base64
import logging
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI, HTTPException, Response

from rescue_ai.application.metrics import render_latest
from rescue_ai.domain.ports import DetectorPort
from rescue_ai.interfaces.detection.schemas import (
    DetectionItem,
    DetectRequest,
    DetectResponse,
    RuntimeInfoResponse,
)

logger = logging.getLogger(__name__)


def build_app(*, detector_factory: Callable[[], DetectorPort]) -> FastAPI:
    """Собрать FastAPI приложение поверх произвольного DetectorPort.

    Тесты передают фейк, прод — фабрику с реальным YoloDetector.
    Lazy-инициализация через factory нужна, чтобы импорт модуля не
    тащил ultralytics в окружения (тесты, CI helm-lint).
    """

    state: dict[str, DetectorPort] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _ = app
        detector = detector_factory()
        try:
            detector.warmup()
        except (ImportError, RuntimeError, ValueError) as err:  # pragma: no cover
            logger.warning("detector warmup failed: %s", err)
        state["detector"] = detector
        yield
        state.clear()

    app = FastAPI(
        title="rescue-ai-detection",
        version="0.1.0",
        lifespan=lifespan,
    )

    def _detector() -> DetectorPort:
        try:
            return state["detector"]
        except KeyError as err:  # pragma: no cover — defensive
            raise HTTPException(
                status_code=503, detail="detector not initialised"
            ) from err

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> dict[str, str]:
        _detector()
        return {"status": "ready"}

    @app.get("/runtime", response_model=RuntimeInfoResponse)
    def runtime_info() -> RuntimeInfoResponse:
        return RuntimeInfoResponse(runtime_name=_detector().runtime_name())

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics() -> Response:
        """Prometheus text-exposition of this service's metrics."""
        payload, content_type = render_latest()
        return Response(content=payload, media_type=content_type)

    @app.post("/detect", response_model=DetectResponse)
    def detect(payload: DetectRequest) -> DetectResponse:
        try:
            jpeg_bytes = base64.b64decode(payload.frame_jpeg_b64, validate=True)
        except (ValueError, TypeError) as err:
            raise HTTPException(
                status_code=400, detail=f"invalid base64: {err}"
            ) from err
        detector = _detector()
        detections = detector.detect(jpeg_bytes)
        return DetectResponse(
            detections=[
                DetectionItem(
                    bbox=list(det.bbox),
                    score=det.score,
                    label=det.label,
                    model_name=det.model_name,
                    explanation=det.explanation,
                )
                for det in detections
            ],
            runtime_name=detector.runtime_name(),
        )

    return app
