from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from libs.batch.infrastructure.detector_runtime import YoloDetectionRuntime


@dataclass
class _RawDetection:
    bbox: tuple[float, float, float, float]
    score: float
    label: str


def test_yolo_detection_runtime_detect(monkeypatch) -> None:
    contract = SimpleNamespace(
        report_provenance=SimpleNamespace(config_hash="cfg-1"),
        alert_rules=SimpleNamespace(),
        inference=SimpleNamespace(),
    )

    class _FakeYoloDetector:
        """Minimal YOLO detector double that returns one detection."""

        def __init__(self, _: object) -> None:
            return

        def predict(self, frame_path) -> list[_RawDetection]:
            _ = frame_path
            return [
                _RawDetection(bbox=(1.0, 2.0, 3.0, 4.0), score=0.91, label="person")
            ]

        def backend_name(self) -> str:
            return "fake-yolo"

    monkeypatch.setattr(
        "libs.batch.infrastructure.detector_runtime.load_stream_contract",
        lambda: contract,
    )
    monkeypatch.setattr(
        "libs.batch.infrastructure.detector_runtime.YoloDetector",
        _FakeYoloDetector,
    )
    runtime = YoloDetectionRuntime(model_version="yolo-v2")
    detections = runtime.detect("/tmp/frame.jpg")

    assert len(detections) == 1
    assert detections[0].label == "person"
    assert detections[0].model_name == "yolo-v2"
    assert detections[0].score > 0.5
