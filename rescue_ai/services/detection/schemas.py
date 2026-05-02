"""Wire schemas for the detection HTTP service (ADR-0008 §1)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DetectRequest(BaseModel):
    """POST /detect — обработать один кадр."""

    frame_jpeg_b64: str = Field(..., description="JPEG-кадр, base64-encoded")


class DetectionItem(BaseModel):
    """Соответствует ``rescue_ai.domain.entities.Detection``."""

    bbox: list[float] = Field(..., min_length=4, max_length=4)
    score: float
    label: str
    model_name: str
    explanation: str | None = None


class DetectResponse(BaseModel):
    detections: list[DetectionItem]
    runtime_name: str


class RuntimeInfoResponse(BaseModel):
    runtime_name: str
