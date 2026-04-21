"""Unit tests for :mod:`rescue_ai.infrastructure.detectors.nanodet_detector`.

The actual torch/nanodet_core dependencies are heavy, so these tests exercise
only the pure-Python slices: frame resolution, detection filtering / mapping,
and the factory selector.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from rescue_ai.application.inference_config import InferenceConfig
from rescue_ai.infrastructure.detectors import build_detector
from rescue_ai.infrastructure.detectors.nanodet_detector import (
    NanoDetDetector,
    NanoDetSettings,
    _build_detections,
    _ensure_asset,
)


def _make_config(detector_name: str = "nanodet", **overrides: Any) -> InferenceConfig:
    base: dict[str, Any] = {
        "model_url": "https://example/nanodet.pth",
        "device": "cpu",
        "imgsz": 416,
        "nms_iou": 0.6,
        "max_det": 100,
        "confidence_threshold": 0.35,
        "detector_name": detector_name,
        "nanodet_config_url": "https://example/nanodet.yml",
    }
    base.update(overrides)
    return InferenceConfig(**base)


def _make_settings(**overrides: Any) -> NanoDetSettings:
    base: dict[str, Any] = {
        "weights_url": "https://example/nanodet.pth",
        "config_url": "https://example/nanodet.yml",
    }
    base.update(overrides)
    return NanoDetSettings(**base)


def test_build_detections_filters_by_confidence_and_labels():
    raw = {
        0: [
            [10.0, 20.0, 30.0, 40.0, 0.9],
            [50.0, 60.0, 70.0, 80.0, 0.1],
        ],
        1: [[0.0, 0.0, 5.0, 5.0, 0.99]],
    }
    detections = _build_detections(
        raw_boxes=raw,
        confidence_threshold=0.5,
        model_name="nanodet-test",
    )
    assert len(detections) == 2
    scores = sorted(d.score for d in detections)
    assert scores == [0.9, 0.99]
    assert all(d.label == "person" for d in detections)
    assert all(d.model_name == "nanodet-test" for d in detections)


def test_build_detections_skips_malformed_rows():
    raw = {0: [[10.0, 20.0, 30.0], [1.0, 2.0, 3.0, 4.0, 0.8]]}
    detections = _build_detections(
        raw_boxes=raw,
        confidence_threshold=0.1,
        model_name="m",
    )
    assert len(detections) == 1
    assert detections[0].bbox == (1.0, 2.0, 3.0, 4.0)


def test_runtime_name_is_nanodet():
    detector = NanoDetDetector(
        config=_make_config(),
        settings=_make_settings(),
    )
    assert detector.runtime_name() == "nanodet"


def test_resolve_frame_rejects_unknown_type():
    detector = NanoDetDetector(
        config=_make_config(),
        settings=_make_settings(),
    )
    with pytest.raises(TypeError):
        detector._resolve_frame(123)


def test_resolve_frame_accepts_ndarray():
    detector = NanoDetDetector(
        config=_make_config(),
        settings=_make_settings(),
    )
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    assert detector._resolve_frame(frame) is frame


def test_factory_builds_yolo_by_default():
    config = _make_config(detector_name="yolo")
    from rescue_ai.infrastructure.detectors import YoloDetector

    detector = build_detector(config)
    assert isinstance(detector, YoloDetector)


def test_factory_builds_nanodet_when_selected():
    detector = build_detector(_make_config())
    assert isinstance(detector, NanoDetDetector)
    assert detector.runtime_name() == "nanodet"


def test_factory_rejects_unknown_detector_name():
    config = _make_config(detector_name="detr")
    with pytest.raises(ValueError, match="Unsupported detector_name"):
        build_detector(config)


def test_factory_requires_nanodet_config_url():
    config = _make_config(nanodet_config_url=None)
    with pytest.raises(ValueError, match="nanodet.config_url"):
        build_detector(config)


def test_ensure_asset_accepts_local_path(tmp_path):
    local = tmp_path / "topology.yml"
    local.write_text("model: {}\n")
    resolved = _ensure_asset(str(local), sha256=None, label="NanoDet config")
    assert resolved == local


def test_ensure_asset_rejects_missing_local_path(tmp_path):
    missing = tmp_path / "does_not_exist.yml"
    with pytest.raises(FileNotFoundError):
        _ensure_asset(str(missing), sha256=None, label="NanoDet config")
