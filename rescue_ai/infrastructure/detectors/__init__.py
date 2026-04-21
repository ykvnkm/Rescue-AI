"""ML detector adapters — concrete ``DetectorPort`` implementations.

Currently shipped:

* ``YoloDetector`` — YOLOv8 via Ultralytics (default).
* ``NanoDetDetector`` — NanoDet-Plus via vendored inference core (P1.4, D2).

Both satisfy ``rescue_ai.domain.ports.DetectorPort``.
"""

from rescue_ai.infrastructure.detectors.factory import build_detector
from rescue_ai.infrastructure.detectors.nanodet_detector import (
    NanoDetDetector,
    NanoDetSettings,
)
from rescue_ai.infrastructure.detectors.yolo_detector import YoloDetector

__all__ = [
    "NanoDetDetector",
    "NanoDetSettings",
    "YoloDetector",
    "build_detector",
]
