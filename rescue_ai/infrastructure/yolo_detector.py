"""YOLOv8 detector wrapper with model download and caching."""

from __future__ import annotations

import hashlib
import importlib
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlretrieve

from rescue_ai.application.inference_config import InferenceConfig
from rescue_ai.domain.entities import Detection

MODEL_CACHE_DIR = Path("runtime/models")
logger = logging.getLogger(__name__)


def _load_yolo_class():
    return getattr(importlib.import_module("ultralytics"), "YOLO")


class YoloDetector:
    """YOLO detector with lazy model loading from public model URL."""

    def __init__(self, config: InferenceConfig, model_version: str = "yolo8n") -> None:
        self._config = config
        self._model_version = model_version
        self._model: Any | None = None

    def detect(self, image_uri: object) -> list[Detection]:
        """Run detection on a single frame and return normalized detections."""
        t0 = time.perf_counter()
        results = self._predict_raw(image_uri)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        if not results:
            logger.debug("YOLO inference: no results (%.1f ms)", elapsed_ms)
            return []

        result = results[0]
        detections = _extract_detections(
            result=result,
            confidence_threshold=self._config.confidence_threshold,
            model_name=self._model_version,
        )
        logger.debug(
            "YOLO inference: detections=%d elapsed=%.1f ms conf_threshold=%.3f",
            len(detections),
            elapsed_ms,
            self._config.confidence_threshold,
        )
        return detections

    def runtime_name(self) -> str:
        """Return human-readable runtime name."""
        return "yolo"

    def _predict_raw(self, image_source: object):
        model = self._ensure_model()
        source = self._resolve_predict_source(image_source)
        return model.predict(
            source=source,
            conf=self._config.confidence_threshold,
            iou=self._config.nms_iou,
            imgsz=self._config.imgsz,
            max_det=self._config.max_det,
            device=self._config.device,
            verbose=False,
        )

    def _resolve_predict_source(self, image_source: object) -> object:
        if isinstance(image_source, Path):
            return str(image_source)
        if isinstance(image_source, str):
            return image_source

        try:
            import numpy as np
        except ImportError as exc:
            raise TypeError("numpy is required for in-memory detection source") from exc

        if isinstance(image_source, np.ndarray):
            return image_source

        if isinstance(image_source, bytes):
            try:
                import cv2
            except ImportError as exc:
                raise TypeError(
                    "opencv-python is required for bytes detection source"
                ) from exc
            frame = cv2.imdecode(
                np.frombuffer(image_source, dtype=np.uint8), cv2.IMREAD_COLOR
            )
            if frame is None:
                raise ValueError("Failed to decode JPEG bytes for detection")
            return frame

        raise TypeError(f"Unsupported image source type: {type(image_source)!r}")

    def warmup(self) -> None:
        self._ensure_model()

    def _ensure_model(self):
        if self._model is not None:
            return self._model

        try:
            yolo_cls = _load_yolo_class()
        except (ImportError, AttributeError) as error:
            raise RuntimeError(
                "ultralytics is not installed.\n"
                "Install: uv sync --extra inference --extra dev"
            ) from error

        model_path = _resolve_model_cache_path(self._config.model_url)
        MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        if not model_path.exists():
            logger.info(
                "Downloading model: %s → %s", self._config.model_url, model_path
            )
            urlretrieve(self._config.model_url, model_path)
            logger.info("Model downloaded: %s", model_path)
        else:
            logger.info("Model cache hit: %s", model_path)

        _verify_model_integrity(
            model_path=model_path,
            expected_sha256=self._config.model_sha256,
        )
        checksum_status = "verified" if self._config.model_sha256 else "skipped"
        logger.info("Model loaded: path=%s checksum=%s", model_path, checksum_status)
        self._model = yolo_cls(str(model_path))
        return self._model


def _resolve_model_cache_path(model_url: str) -> Path:
    parsed = urlparse(model_url)
    filename = Path(parsed.path).name or "model.pt"
    return MODEL_CACHE_DIR / filename


def _verify_model_integrity(model_path: Path, expected_sha256: str | None) -> None:
    if not expected_sha256:
        return
    normalized = expected_sha256.strip().lower()
    if len(normalized) != 64 or not all(ch in "0123456789abcdef" for ch in normalized):
        raise RuntimeError("Invalid model_sha256 format in runtime config")
    actual = hashlib.sha256(model_path.read_bytes()).hexdigest()
    if actual != normalized:
        message = (
            f"Model checksum mismatch for {model_path.name}: "
            f"expected {normalized}, got {actual}"
        )
        raise RuntimeError(message)


def _extract_detections(
    result, confidence_threshold: float, model_name: str = "yolo8n"
) -> list[Detection]:
    boxes = result.boxes
    names = result.names

    if boxes is None:
        return []

    person_ids = _resolve_person_ids(names)
    cls_ids = boxes.cls.cpu().numpy().astype(int)
    scores = boxes.conf.cpu().numpy()
    coords = boxes.xyxy.cpu().numpy()

    detections: list[Detection] = []
    for box, score, cls_id in zip(coords, scores, cls_ids):
        if person_ids and cls_id not in person_ids:
            continue
        if float(score) < confidence_threshold:
            continue

        detections.append(
            Detection(
                bbox=(
                    float(box[0]),
                    float(box[1]),
                    float(box[2]),
                    float(box[3]),
                ),
                score=float(score),
                label="person",
                model_name=model_name,
            )
        )

    return detections


def _resolve_person_ids(names: dict[int, str] | list[str]) -> set[int]:
    if isinstance(names, dict):
        return {idx for idx, name in names.items() if str(name).lower() == "person"}

    return {idx for idx, name in enumerate(names) if str(name).lower() == "person"}
