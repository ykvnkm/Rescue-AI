"""Tests for detector-selection plumbing in the stream contract loader."""

from __future__ import annotations

import pytest

from rescue_ai.infrastructure.contract_loader import _build_inference_config


def test_detector_defaults_to_yolo_when_field_missing():
    payload = {
        "model_url": "https://example/yolo.pt",
        "device": "cpu",
        "infer": {"imgsz": 960, "nms_iou": 0.75, "max_det": 1000},
    }
    cfg = _build_inference_config(payload, confidence_threshold=0.2)
    assert cfg.detector_name == "yolo"
    assert cfg.nanodet_config_url is None
    assert cfg.nanodet_onnx_url is None


def test_detector_nanodet_with_nested_config():
    payload = {
        "model_url": "https://example/nanodet.pth",
        "model_sha256": "ABC",
        "device": "cpu",
        "detector": {
            "name": "nanodet",
            "nanodet": {
                "config_url": "https://example/cfg.yml",
                "config_sha256": "DEF",
                "onnx_url": "https://example/model.onnx",
                "onnx_sha256": "123",
            },
        },
        "infer": {"imgsz": 416, "nms_iou": 0.6, "max_det": 100},
    }
    cfg = _build_inference_config(payload, confidence_threshold=0.35)
    assert cfg.detector_name == "nanodet"
    assert cfg.model_sha256 == "abc"
    assert cfg.nanodet_config_url == "https://example/cfg.yml"
    assert cfg.nanodet_config_sha256 == "def"
    assert cfg.nanodet_onnx_url == "https://example/model.onnx"
    assert cfg.nanodet_onnx_sha256 == "123"


def test_detector_rejects_unknown_name():
    payload = {"detector": {"name": "detr"}, "infer": {}}
    with pytest.raises(ValueError, match="Unsupported detector"):
        _build_inference_config(payload, confidence_threshold=0.2)
