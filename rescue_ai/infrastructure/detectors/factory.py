"""Factory for concrete ``DetectorPort`` implementations by runtime."""

from __future__ import annotations

from rescue_ai.application.inference_config import InferenceConfig
from rescue_ai.infrastructure.detectors.yolo_detector import (
    NcnnYoloDetector,
    PtYoloDetector,
)


def build_detector(config: InferenceConfig):
    """Return a detector adapter wired from ``InferenceConfig.runtime``."""
    if config.runtime == "pt":
        return PtYoloDetector(config=config)
    if config.runtime == "ncnn":
        return NcnnYoloDetector(config=config)
    raise ValueError(f"Unsupported model runtime: {config.runtime!r}")
