"""Tests for runtime contract, annotation loading and detector helpers."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from rescue_ai.application.inference_config import InferenceConfig
from rescue_ai.application.payloads import build_frame_payload, serialize_detections
from rescue_ai.infrastructure.annotation_index import build_annotation_index
from rescue_ai.infrastructure.yolo_detector import YoloDetector, _resolve_person_ids


def test_build_annotation_index_from_coco_json() -> None:
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        images_dir = root / "images"
        annotations_dir = root / "annotations"
        images_dir.mkdir()
        annotations_dir.mkdir()

        (images_dir / "frame_0001.jpg").write_bytes(b"\xff\xd8\xff\xd9")

        payload = {
            "images": [{"id": 1, "file_name": "frame_0001.jpg"}],
            "categories": [{"id": 1, "name": "person"}],
            "annotations": [
                {"image_id": 1, "category_id": 1, "bbox": [10, 20, 30, 40]}
            ],
        }

        (annotations_dir / "mission.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

        index = build_annotation_index(images_dir, explicit_path=None)
        boxes = index.get_gt_boxes(images_dir / "frame_0001.jpg")

        assert index.has_frame(images_dir / "frame_0001.jpg")
        assert boxes == [(10.0, 20.0, 40.0, 60.0)]


def test_serialize_detections_and_payload() -> None:
    detections = [
        SimpleNamespace(bbox=(1.0, 2.0, 3.0, 4.0), score=0.9),
        SimpleNamespace(bbox=(5.0, 6.0, 7.0, 8.0), score=0.8),
    ]

    serialized = serialize_detections(
        detections=detections,
        min_detections_per_frame=2,
    )

    payload = build_frame_payload(
        frame_id=1,
        ts_sec=0.5,
        frame_path=Path("/tmp/frame_0001.jpg"),
        gt_boxes=[(0.0, 0.0, 1.0, 1.0)],
        detections=serialized,
    )

    assert len(serialized) == 2
    assert payload["gt_person_present"] is True
    assert payload["detections"] == serialized


def test_resolve_person_ids() -> None:
    assert _resolve_person_ids({0: "person", 1: "car"}) == {0}
    assert _resolve_person_ids(["car", "person"]) == {1}


def test_yolo_detector_warmup_requires_ultralytics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_url = (
        "https://storage.yandexcloud.net/rescue-ai-models-public/models/"
        "yolov8n_baseline_multiscale/v1/yolov8n_baseline_multiscale.pt"
    )
    config = InferenceConfig(
        model_url=model_url,
        device="cpu",
        imgsz=960,
        nms_iou=0.75,
        max_det=1000,
        confidence_threshold=0.2,
    )

    detector = YoloDetector(config)

    monkeypatch.setattr(
        "rescue_ai.infrastructure.yolo_detector.YOLO",
        None,
    )

    with pytest.raises(RuntimeError):
        detector.warmup()
