from __future__ import annotations

from pathlib import Path

from services.detection_service.domain.models import DetectionResult, InferenceConfig
from services.detection_service.infrastructure.s3_artifact_storage import (
    S3ArtifactStorage,
)

try:
    from ultralytics import YOLO
except ImportError:
    YOLO = None


RUNTIME_ROOT = Path("runtime")


class YoloDetector:
    """YOLO detector with lazy model loading from S3-backed object storage."""

    def __init__(self, config: InferenceConfig) -> None:
        self._config = config
        self._model = None

    def predict(self, frame_path: Path) -> list[DetectionResult]:
        results = self._predict_raw(frame_path)
        if not results:
            return []

        result = results[0]
        return _extract_detections(
            result=result,
            confidence_threshold=self._config.confidence_threshold,
        )

    def _predict_raw(self, frame_path: Path):
        model = self._ensure_model()
        return model.predict(
            source=str(frame_path),
            conf=self._config.confidence_threshold,
            iou=self._config.nms_iou,
            imgsz=self._config.imgsz,
            max_det=self._config.max_det,
            device=self._config.device,
            verbose=False,
        )

    def warmup(self) -> None:
        self._ensure_model()

    def _ensure_model(self):
        if self._model is not None:
            return self._model

        if YOLO is None:
            raise RuntimeError(
                "ultralytics не установлен.\n"
                "Установи: uv sync --extra inference"
            )

        object_key = self._config.model_path
        local_model_path = _resolve_local_model_path(object_key)

        storage = S3ArtifactStorage.from_env()
        resolved_model_path = storage.download_model_if_needed(
            object_key=object_key,
            local_path=str(local_model_path),
        )

        self._model = YOLO(str(resolved_model_path))
        return self._model


def _resolve_local_model_path(object_key: str) -> Path:
    normalized_key = object_key.lstrip("/").replace("\\", "/")
    return RUNTIME_ROOT / normalized_key


def _extract_detections(result, confidence_threshold: float) -> list[DetectionResult]:
    boxes = result.boxes
    names = result.names

    if boxes is None:
        return []

    person_ids = _resolve_person_ids(names)
    cls_ids = boxes.cls.cpu().numpy().astype(int)
    scores = boxes.conf.cpu().numpy()
    coords = boxes.xyxy.cpu().numpy()

    detections: list[DetectionResult] = []
    for box, score, cls_id in zip(coords, scores, cls_ids):
        if person_ids and cls_id not in person_ids:
            continue
        if float(score) < confidence_threshold:
            continue

        detections.append(
            DetectionResult(
                bbox=(
                    float(box[0]),
                    float(box[1]),
                    float(box[2]),
                    float(box[3]),
                ),
                score=float(score),
            )
        )

    return detections


def _resolve_person_ids(names: dict[int, str] | list[str]) -> set[int]:
    if isinstance(names, dict):
        return {idx for idx, name in names.items() if str(name).lower() == "person"}

    return {idx for idx, name in enumerate(names) if str(name).lower() == "person"}