"""Tests for YoloDetector.detect() with mocked inference."""

from __future__ import annotations

from types import SimpleNamespace

np = __import__("pytest").importorskip("numpy")

from rescue_ai.domain.entities import InferenceConfig  # noqa: E402
from rescue_ai.infrastructure.yolo_detector import YoloDetector  # noqa: E402


def _fake_result(
    bboxes: list[list[float]],
    scores: list[float],
    cls_ids: list[int],
    names: dict[int, str],
) -> SimpleNamespace:
    """Build a fake ultralytics result with .boxes and .names."""
    return SimpleNamespace(
        boxes=SimpleNamespace(
            cls=SimpleNamespace(
                cpu=lambda: SimpleNamespace(numpy=lambda: np.array(cls_ids, dtype=int))
            ),
            conf=SimpleNamespace(
                cpu=lambda: SimpleNamespace(numpy=lambda: np.array(scores))
            ),
            xyxy=SimpleNamespace(
                cpu=lambda: SimpleNamespace(numpy=lambda: np.array(bboxes))
            ),
        ),
        names=names,
    )


def test_yolo_detector_detect(monkeypatch) -> None:
    config = InferenceConfig(
        model_url="http://example.com/model.pt",
        device="cpu",
        imgsz=960,
        nms_iou=0.75,
        max_det=1000,
        confidence_threshold=0.2,
    )
    detector = YoloDetector(config=config, model_version="yolo-v2")

    result = _fake_result(
        bboxes=[[1.0, 2.0, 3.0, 4.0]],
        scores=[0.91],
        cls_ids=[0],
        names={0: "person"},
    )
    monkeypatch.setattr(detector, "_predict_raw", lambda _path: [result])

    detections = detector.detect("/tmp/frame.jpg")

    assert len(detections) == 1
    assert detections[0].label == "person"
    assert detections[0].model_name == "yolo-v2"
    assert detections[0].score > 0.5
    assert detections[0].bbox == (1.0, 2.0, 3.0, 4.0)
