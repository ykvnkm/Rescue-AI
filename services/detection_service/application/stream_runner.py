from services.detection_service.application.annotation_index import (
    AnnotationIndex,
    build_annotation_index,
)
from services.detection_service.application.payloads import (
    build_frame_payload,
    serialize_detections,
)

# Backward-compatible aliases kept for current tests and imports.
_build_frame_payload = build_frame_payload
_serialize_detections = serialize_detections

__all__ = [
    "AnnotationIndex",
    "_build_frame_payload",
    "_serialize_detections",
    "build_annotation_index",
]
