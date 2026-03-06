from __future__ import annotations

from services.detection_service.application.frame_source import FrameSourceService
from services.detection_service.application.stream_config import (
    StreamConfig,
    StreamOptions,
)
from services.detection_service.application.stream_config import (
    build_stream_config as build_stream_config_use_case,
)
from services.detection_service.application.stream_orchestrator import (
    StreamOrchestrator,
    StreamState,
)
from services.detection_service.infrastructure.http_publisher import HttpFramePublisher
from services.detection_service.infrastructure.runtime_contract import (
    load_stream_contract,
)
from services.detection_service.infrastructure.yolo_detector import YoloDetector

_frame_source = FrameSourceService()
_orchestrator = StreamOrchestrator(
    detector_factory=YoloDetector,
    frame_publisher=HttpFramePublisher(),
    frame_source=_frame_source,
)


def build_stream_config(mission_id: str, options: StreamOptions) -> StreamConfig:
    contract = load_stream_contract()
    return build_stream_config_use_case(
        mission_id=mission_id,
        options=options,
        contract=contract,
        frame_source=_frame_source,
    )


def get_stream_state(mission_id: str) -> StreamState | None:
    return _orchestrator.get_stream_state(mission_id)


def start_stream(config: StreamConfig) -> StreamState:
    return _orchestrator.start_stream(config)


def stop_stream(mission_id: str) -> StreamState | None:
    return _orchestrator.stop_stream(mission_id)


def wait_stream_stopped(
    mission_id: str,
    timeout_sec: float = 3.0,
) -> StreamState | None:
    return _orchestrator.wait_stream_stopped(
        mission_id=mission_id, timeout_sec=timeout_sec
    )
