from __future__ import annotations

from pathlib import Path
from typing import Protocol

from services.detection_service.domain.models import DetectionResult


class DetectorPort(Protocol):
    """Port for ML detector used by stream application service."""

    def predict(self, frame_path: Path) -> list[DetectionResult]: ...
    def warmup(self) -> None: ...


class FramePublisherPort(Protocol):
    """Port for publishing frame payload into mission API."""

    def publish(
        self, mission_id: str, api_base: str, payload: dict[str, object]
    ) -> None: ...
    def endpoint(self, mission_id: str, api_base: str) -> str: ...
