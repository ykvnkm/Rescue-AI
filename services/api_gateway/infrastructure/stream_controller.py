from __future__ import annotations

from typing import Any

from services.detection_service.application.stream_config import StreamOptions
from services.detection_service.application.stream_orchestrator import (
    StreamState as StreamStateView,
)
from services.detection_service.infrastructure.stream_runtime_api import (
    build_stream_config,
    get_stream_state,
    start_stream,
    stop_stream,
    wait_stream_stopped,
)


class DetectionStreamController:
    """Adapter from api_gateway to detection_service stream API."""

    def build_config(self, mission_id: str, options: StreamOptions) -> Any:
        return build_stream_config(mission_id=mission_id, options=options)

    def start(self, config: Any) -> StreamStateView:
        state = start_stream(config)
        return _as_state_view(state)

    def stop(self, mission_id: str) -> StreamStateView | None:
        state = stop_stream(mission_id)
        return _as_state_view(state) if state is not None else None

    def wait_stopped(
        self, mission_id: str, timeout_sec: float = 3.0
    ) -> StreamStateView | None:
        state = wait_stream_stopped(mission_id=mission_id, timeout_sec=timeout_sec)
        return _as_state_view(state) if state is not None else None

    def get_state(self, mission_id: str) -> StreamStateView | None:
        state = get_stream_state(mission_id)
        return _as_state_view(state) if state is not None else None


def _as_state_view(state: Any) -> StreamStateView:
    return StreamStateView(
        mission_id=str(state.mission_id),
        running=bool(state.running),
        processed_frames=int(state.processed_frames),
        total_frames=int(state.total_frames),
        last_frame_name=state.last_frame_name,
        error=state.error,
        stop_requested=bool(state.stop_requested),
    )
