"""Inference runtime configuration.

``InferenceConfig`` describes adapter-level ML runtime settings
(model URL, device, image size, NMS parameters).  It is *not* a domain
value object because these fields are specific to the YOLO adapter
rather than business rules.  Placed in the application layer so that
both infrastructure adapters and application orchestrators
(``StreamOrchestrator``) can depend on it without polluting the domain.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InferenceConfig:
    """YOLO inference runtime settings resolved from external contract/config."""

    model_url: str
    device: str
    imgsz: int
    nms_iou: float
    max_det: int
    confidence_threshold: float
