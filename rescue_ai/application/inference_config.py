"""Inference runtime configuration.

``InferenceConfig`` describes adapter-level ML runtime settings
(model URL, device, image size, NMS parameters).  It is *not* a domain
value object because these fields are specific to detection adapters
rather than business rules.  Placed in the application layer so that
both infrastructure adapters and application orchestrators
(``StreamOrchestrator``) can depend on it without polluting the domain.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InferenceConfig:
    """Detector-agnostic inference runtime settings from external contract."""

    model_url: str
    device: str
    imgsz: int
    nms_iou: float
    max_det: int
    confidence_threshold: float
    model_sha256: str | None = None
    detector_name: str = "yolo"
    nanodet_config_url: str | None = None
    nanodet_config_sha256: str | None = None
    nanodet_onnx_url: str | None = None
    nanodet_onnx_sha256: str | None = None
