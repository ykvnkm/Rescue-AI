"""Tests for detector runtime selection."""

from __future__ import annotations

import pytest

from rescue_ai.application.inference_config import InferenceConfig
from rescue_ai.infrastructure.detectors import (
    NcnnYoloDetector,
    PtYoloDetector,
    build_detector,
)


def _config(runtime: str = "pt") -> InferenceConfig:
    return InferenceConfig(
        runtime=runtime,  # type: ignore[arg-type]
        pt_model_url="https://example.com/model.pt",
        ncnn_model_url="https://example.com/model_ncnn.zip",
        device="cpu",
        imgsz=960,
        nms_iou=0.5,
        max_det=1000,
        confidence_threshold=0.3,
    )


def test_factory_builds_pt_runtime_by_default() -> None:
    detector = build_detector(_config("pt"))
    assert isinstance(detector, PtYoloDetector)


def test_factory_builds_ncnn_runtime() -> None:
    detector = build_detector(_config("ncnn"))
    assert isinstance(detector, NcnnYoloDetector)


def test_factory_rejects_unknown_runtime() -> None:
    with pytest.raises(ValueError, match="Unsupported model runtime"):
        build_detector(_config("tensorrt"))
