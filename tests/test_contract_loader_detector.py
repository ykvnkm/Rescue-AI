"""Tests for detector-selection plumbing in the stream contract loader."""

from __future__ import annotations

import pytest

from rescue_ai.infrastructure.contract_loader import _build_inference_config


def test_runtime_defaults_to_pt_when_field_missing():
    payload = {
        "model_url": "https://example/yolo.pt",
        "device": "cpu",
        "infer": {"imgsz": 960, "nms_iou": 0.75, "max_det": 1000},
    }
    cfg = _build_inference_config(payload, confidence_threshold=0.2)
    assert cfg.runtime == "pt"
    assert cfg.pt_model_url == "https://example/yolo.pt"
    assert cfg.model_url == "https://example/yolo.pt"


def test_runtime_ncnn_with_nested_model_config():
    payload = {
        "device": "cpu",
        "model": {
            "runtime": "ncnn",
            "pt_url": "https://example/model.pt",
            "pt_sha256": "ABC",
            "ncnn_url": "https://example/model_ncnn.zip",
            "ncnn_sha256": "DEF",
        },
        "infer": {"imgsz": 416, "nms_iou": 0.6, "max_det": 100},
    }
    cfg = _build_inference_config(payload, confidence_threshold=0.35)
    assert cfg.runtime == "ncnn"
    assert cfg.pt_model_url == "https://example/model.pt"
    assert cfg.pt_model_sha256 == "abc"
    assert cfg.ncnn_model_url == "https://example/model_ncnn.zip"
    assert cfg.ncnn_model_sha256 == "def"
    assert cfg.model_url == "https://example/model_ncnn.zip"


def test_runtime_rejects_unknown_name():
    payload = {"model": {"runtime": "onnx"}, "infer": {}}
    with pytest.raises(ValueError, match="Unsupported model runtime"):
        _build_inference_config(payload, confidence_threshold=0.2)


def test_runtime_ncnn_requires_model_url():
    payload = {"model": {"runtime": "ncnn"}, "infer": {}}
    with pytest.raises(ValueError, match="ncnn_url"):
        _build_inference_config(payload, confidence_threshold=0.2)
