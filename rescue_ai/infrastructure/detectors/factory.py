"""Factory for concrete ``DetectorPort`` implementations by name."""

from __future__ import annotations

from rescue_ai.application.inference_config import InferenceConfig
from rescue_ai.infrastructure.detectors.nanodet_detector import (
    NanoDetDetector,
    NanoDetSettings,
)
from rescue_ai.infrastructure.detectors.yolo_detector import YoloDetector


def build_detector(config: InferenceConfig):
    """Return a detector adapter wired from ``InferenceConfig.detector_name``."""
    name = config.detector_name
    if name == "yolo":
        return YoloDetector(config=config)
    if name == "nanodet":
        if not config.nanodet_config_url:
            raise ValueError(
                "NanoDet detector requires detector.nanodet.config_url in contract"
            )
        settings = NanoDetSettings(
            weights_url=config.model_url,
            config_url=config.nanodet_config_url,
            weights_sha256=config.model_sha256,
            config_sha256=config.nanodet_config_sha256,
            onnx_url=config.nanodet_onnx_url,
            onnx_sha256=config.nanodet_onnx_sha256,
        )
        return NanoDetDetector(config=config, settings=settings)
    raise ValueError(f"Unsupported detector_name: {name!r}")
