"""ML detector adapters — concrete ``DetectorPort`` implementations.

Currently shipped:

* ``PtYoloDetector`` — YOLOv8 via PyTorch weights for stand processing.
* ``NcnnYoloDetector`` — YOLOv8 via NCNN export for local streaming.

Both satisfy ``rescue_ai.domain.ports.DetectorPort``.
"""

from rescue_ai.infrastructure.detectors.factory import build_detector
from rescue_ai.infrastructure.detectors.yolo_detector import (
    NcnnYoloDetector,
    PtYoloDetector,
    YoloDetector,
)

__all__ = [
    "NcnnYoloDetector",
    "PtYoloDetector",
    "YoloDetector",
    "build_detector",
]
