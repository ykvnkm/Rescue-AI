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
from typing import Literal

ModelRuntime = Literal["pt", "ncnn"]


@dataclass(frozen=True)
class InferenceConfig:
    """Detector-agnostic inference runtime settings from external contract."""

    runtime: ModelRuntime
    pt_model_url: str
    device: str
    imgsz: int
    nms_iou: float
    max_det: int
    confidence_threshold: float
    pt_model_sha256: str | None = None
    ncnn_model_url: str | None = None
    ncnn_model_sha256: str | None = None

    @property
    def model_url(self) -> str:
        """Return the model URL selected by the configured runtime."""
        if self.runtime == "ncnn":
            return self.ncnn_model_url or self.pt_model_url
        return self.pt_model_url

    @property
    def model_sha256(self) -> str | None:
        """Return the checksum selected by the configured runtime."""
        if self.runtime == "ncnn":
            return self.ncnn_model_sha256
        return self.pt_model_sha256

    @property
    def detector_name(self) -> str:
        """Return the logical detector family name used in reports."""
        return "yolo"
