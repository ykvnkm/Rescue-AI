from __future__ import annotations

from libs.batch.infrastructure.detector_runtime import FakeDetectionRuntime


def test_fake_detection_runtime_detect() -> None:
    runtime = FakeDetectionRuntime(model_version="fake-v2")
    detections = runtime.detect("/tmp/frame.jpg")

    assert len(detections) == 1
    assert detections[0].label == "person"
    assert detections[0].model_name == "fake-v2"
    assert detections[0].score > 0.5
