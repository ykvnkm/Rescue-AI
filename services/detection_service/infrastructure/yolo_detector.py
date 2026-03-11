from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlretrieve

from services.detection_service.domain.models import DetectionResult, InferenceConfig

try:
    from ultralytics import YOLO
except ImportError:
    YOLO = None

MODEL_CACHE_DIR = Path("runtime/models")


class YoloDetector:
    """YOLO detector with lazy model loading from public model URL."""

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
                "Установи: uv sync --extra inference --extra dev"
            )

        model_path = _resolve_model_cache_path(self._config.model_url)
        MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        if not model_path.exists():
            urlretrieve(self._config.model_url, model_path)

        self._model = YOLO(str(model_path))
        return self._model


def _resolve_model_cache_path(model_url: str) -> Path:
    parsed = urlparse(model_url)
    filename = Path(parsed.path).name or "model.pt"
    return MODEL_CACHE_DIR / filename


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