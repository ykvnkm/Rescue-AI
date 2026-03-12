from __future__ import annotations

from services.detection_service.application.stream_config import (
    StreamConfig,
    StreamOptions,
)
from services.detection_service.application.stream_orchestrator import StreamState
from services.detection_service.infrastructure.stream_runtime_api import (
    build_stream_config as app_build_stream_config,
)
from services.detection_service.infrastructure.stream_runtime_api import (
    get_stream_state as app_get_stream_state,
)
from services.detection_service.infrastructure.stream_runtime_api import (
    start_stream as app_start_stream,
)
from services.detection_service.infrastructure.stream_runtime_api import (
    stop_stream as app_stop_stream,
)
from services.detection_service.infrastructure.stream_runtime_api import (
    wait_stream_stopped as app_wait_stream_stopped,
)


def build_stream_config(mission_id: str, options: StreamOptions) -> StreamConfig:
    return app_build_stream_config(mission_id=mission_id, options=options)


def get_stream_state(mission_id: str) -> StreamState | None:
    return app_get_stream_state(mission_id)


def start_stream(config: StreamConfig) -> StreamState:
    return app_start_stream(config)


def stop_stream(mission_id: str) -> StreamState | None:
    return app_stop_stream(mission_id)


def wait_stream_stopped(
    mission_id: str,
    timeout_sec: float = 3.0,
) -> StreamState | None:
    return app_wait_stream_stopped(mission_id=mission_id, timeout_sec=timeout_sec)
