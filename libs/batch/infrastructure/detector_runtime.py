from __future__ import annotations

from pathlib import Path

from libs.core.application.models import DetectionInput
from services.detection_service.infrastructure.runtime_contract import (
    load_stream_contract,
)
from services.detection_service.infrastructure.yolo_detector import YoloDetector

# pylint: disable=too-few-public-methods,missing-class-docstring


class YoloDetectionRuntime:
    def __init__(self, model_version: str = "yolov8n_baseline_multiscale") -> None:
        self._contract = load_stream_contract()
        self._detector = YoloDetector(self._contract.inference)
        self._model_version = model_version

    @property
    def config_hash(self) -> str:
        return self._contract.report_provenance.config_hash

    @property
    def rules(self):
        return self._contract.alert_rules

    def detect(self, image_uri: str) -> list[DetectionInput]:
        detections = self._detector.predict(frame_path=_to_path(image_uri))
        return [
            DetectionInput(
                bbox=item.bbox,
                score=item.score,
                label=item.label,
                model_name=self._model_version,
                explanation="batch-yolo-inference",
            )
            for item in detections
        ]


def _to_path(image_uri: str) -> Path:
    return Path(image_uri)
