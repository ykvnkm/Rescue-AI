"""HTTP-адаптер для ``DetectorPort`` (ADR-0008 §1).

Принимает любой image source, который умеет принимать
``YoloDetector.detect`` (numpy array, bytes, Path, str-URI). Перед
отправкой адаптер сводит вход к JPEG-байтам и кодирует их в base64.

В отличие от nav-engine, детектор stateless — ``warmup`` сводится к
GET /health (просто проверяем, что под жив), за runtime_name тоже
ходим один раз и кэшируем.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

import cv2
import httpx
import numpy as np

from rescue_ai.domain.entities import Detection

logger = logging.getLogger(__name__)


class HttpDetector:
    """``DetectorPort`` поверх HTTP."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_sec: float = 5.0,
        client: httpx.Client | None = None,
        jpeg_quality: int = 85,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_sec
        self._jpeg_quality = int(max(50, min(95, jpeg_quality)))
        self._client = client or httpx.Client(timeout=timeout_sec)
        self._runtime_name_cache: str | None = None

    # ── DetectorPort ────────────────────────────────────────────

    def detect(self, image_uri: object) -> list[Detection]:
        b64 = _to_jpeg_b64(image_uri, self._jpeg_quality)
        response = self._client.post(
            f"{self._base_url}/detect",
            json={"frame_jpeg_b64": b64},
            timeout=self._timeout,
        )
        response.raise_for_status()
        body = response.json()
        detections = [
            _detection_from_payload(item)
            for item in body.get("detections", [])
        ]
        runtime_name = body.get("runtime_name")
        if isinstance(runtime_name, str) and runtime_name:
            self._runtime_name_cache = runtime_name
        return detections

    def warmup(self) -> None:
        # Pod warmup делает сам сервис в lifespan; адаптеру достаточно
        # убедиться, что сетевой канал жив.
        try:
            response = self._client.get(
                f"{self._base_url}/health",
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as err:
            logger.warning("HttpDetector warmup failed: %s", err)

    def runtime_name(self) -> str:
        if self._runtime_name_cache is not None:
            return self._runtime_name_cache
        try:
            response = self._client.get(
                f"{self._base_url}/runtime",
                timeout=self._timeout,
            )
            response.raise_for_status()
            self._runtime_name_cache = str(
                response.json().get("runtime_name", "remote")
            )
        except httpx.HTTPError as err:
            logger.warning("HttpDetector runtime_name probe failed: %s", err)
            self._runtime_name_cache = "remote"
        return self._runtime_name_cache

    def close(self) -> None:
        self._client.close()


def _to_jpeg_b64(image_source: object, quality: int) -> str:
    if isinstance(image_source, (bytes, bytearray)):
        return base64.b64encode(bytes(image_source)).decode("ascii")
    if isinstance(image_source, Path):
        with open(image_source, "rb") as fh:
            return base64.b64encode(fh.read()).decode("ascii")
    if isinstance(image_source, str):
        # Локальный файл; URI на S3/HTTP не поддерживаем — это работа
        # вызывающего слоя (он уже скачивает кадры в bytes).
        with open(image_source, "rb") as fh:
            return base64.b64encode(fh.read()).decode("ascii")
    if isinstance(image_source, np.ndarray):
        success, buffer = cv2.imencode(
            ".jpg",
            image_source,
            [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)],
        )
        if not success:
            raise RuntimeError("cv2.imencode failed for detector frame")
        return base64.b64encode(buffer.tobytes()).decode("ascii")
    raise TypeError(
        f"HttpDetector: unsupported image source type {type(image_source)!r}"
    )


def _detection_from_payload(item: dict[str, Any]) -> Detection:
    bbox = item.get("bbox") or [0.0, 0.0, 0.0, 0.0]
    return Detection(
        bbox=(
            float(bbox[0]),
            float(bbox[1]),
            float(bbox[2]),
            float(bbox[3]),
        ),
        score=float(item.get("score", 0.0)),
        label=str(item.get("label", "person")),
        model_name=str(item.get("model_name", "remote")),
        explanation=(
            None
            if item.get("explanation") is None
            else str(item.get("explanation"))
        ),
    )


__all__ = ["HttpDetector"]
