"""Tests for runtime contract, annotation loading and detector helpers."""

import hashlib
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

    def _raise_import_error():
        raise ImportError("missing ultralytics")

    monkeypatch.setattr(
        "rescue_ai.infrastructure.yolo_detector._load_yolo_class",
        _raise_import_error,
    )

    with pytest.raises(RuntimeError):
        detector.warmup()


def test_yolo_detector_validates_checksum(monkeypatch: pytest.MonkeyPatch) -> None:
    with TemporaryDirectory() as temp_dir:
        cache_dir = Path(temp_dir) / "models"
        cache_dir.mkdir(parents=True, exist_ok=True)
        model_file = cache_dir / "model.pt"
        model_file.write_bytes(b"fake-model")

        monkeypatch.setattr(
            "rescue_ai.infrastructure.yolo_detector.MODEL_CACHE_DIR", cache_dir
        )

        config = InferenceConfig(
            model_url="https://example.com/model.pt",
            device="cpu",
            imgsz=640,
            nms_iou=0.7,
            max_det=100,
            confidence_threshold=0.25,
            model_sha256="0" * 64,
        )
        detector = YoloDetector(config)

        class _FakeYolo:
            def __init__(self, model_path: str) -> None:
                _ = model_path

        monkeypatch.setattr(
            "rescue_ai.infrastructure.yolo_detector._load_yolo_class",
            lambda: _FakeYolo,
        )

        with pytest.raises(RuntimeError, match="Model checksum mismatch"):
            detector.warmup()


def test_yolo_detector_accepts_matching_checksum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as temp_dir:
        cache_dir = Path(temp_dir) / "models"
        cache_dir.mkdir(parents=True, exist_ok=True)
        model_file = cache_dir / "model.pt"
        payload = b"fake-model"
        model_file.write_bytes(payload)
        expected_hash = hashlib.sha256(payload).hexdigest()

        monkeypatch.setattr(
            "rescue_ai.infrastructure.yolo_detector.MODEL_CACHE_DIR", cache_dir
        )

        config = InferenceConfig(
            model_url="https://example.com/model.pt",
            device="cpu",
            imgsz=640,
            nms_iou=0.7,
            max_det=100,
            confidence_threshold=0.25,
            model_sha256=expected_hash,
        )
        detector = YoloDetector(config)

        class _FakeYolo:
            def __init__(self, model_path: str) -> None:
                self.model_path = model_path

        monkeypatch.setattr(
            "rescue_ai.infrastructure.yolo_detector._load_yolo_class",
            lambda: _FakeYolo,
        )

        detector.warmup()
