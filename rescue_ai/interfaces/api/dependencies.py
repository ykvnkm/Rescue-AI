"""FastAPI dependency providers backed by runtime state.

No infrastructure wiring lives in this module.
Runtime dependencies are injected by the entrypoint and lazily
initialized as a fallback for local tests.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Callable, Protocol

from rescue_ai.application.auto_mission_service import AutoMissionService
from rescue_ai.application.pilot_service import PilotService
from rescue_ai.domain.entities import Detection
from rescue_ai.domain.ports import ArtifactStorage


class StreamControllerPort(Protocol):
    """Minimal stream controller contract consumed by API routes."""

    def start(
        self,
        *,
        mission_id: str,
        rpi_mission_id: str,
        target_fps: float,
    ) -> object: ...

    def stop(self, mission_id: str) -> StreamStopState | None: ...

    def as_payload(self, mission_id: str) -> dict[str, object] | None: ...

    def check_rpi_health(self) -> dict[str, object]: ...

    def list_rpi_missions(self) -> list[dict[str, str]]: ...


class DetectorPort(Protocol):
    """Single-frame detector contract consumed by /predict endpoint."""

    def detect(self, image_uri: object) -> list[Detection]: ...


class StreamStopState(Protocol):
    """Subset of stream state used by API completion/status routes."""

    processed_frames: int
    error: str | None
    end_reason: str | None


@dataclass
class ApiRuntime:
    """Runtime dependency bundle for API route handlers."""

    pilot_service: PilotService
    stream_controller: StreamControllerPort
    reset_hook: Callable[[], None]
    detector: DetectorPort | None = field(default=None)
    artifact_storage: ArtifactStorage | None = field(default=None)
    auto_mission_service: AutoMissionService | None = field(default=None)


@dataclass
class _RuntimeState:
    runtime: ApiRuntime | None = None


_STATE = _RuntimeState()


def set_runtime(runtime: ApiRuntime) -> None:
    """Install runtime dependencies for API requests."""
    _STATE.runtime = runtime


def _ensure_runtime() -> ApiRuntime:
    if _STATE.runtime is None:
        build_api_runtime = getattr(
            importlib.import_module("rescue_ai.interfaces.cli.online"),
            "build_api_runtime",
        )
        runtime_parts = build_api_runtime()
        auto_mission_service: AutoMissionService | None = None
        if len(runtime_parts) == 4:
            pilot_service, stream_controller, reset_hook, detector = runtime_parts
            artifact_storage = None
        elif len(runtime_parts) == 5:
            (
                pilot_service,
                stream_controller,
                reset_hook,
                detector,
                artifact_storage,
            ) = runtime_parts
        else:
            (
                pilot_service,
                stream_controller,
                reset_hook,
                detector,
                artifact_storage,
                auto_mission_service,
            ) = runtime_parts
        _STATE.runtime = ApiRuntime(
            pilot_service=pilot_service,
            stream_controller=stream_controller,
            reset_hook=reset_hook,
            detector=detector,
            artifact_storage=artifact_storage,
            auto_mission_service=auto_mission_service,
        )
    return _STATE.runtime


def get_container() -> ApiRuntime:
    """Return initialized API runtime container."""
    return _ensure_runtime()


def get_pilot_service() -> PilotService:
    return _ensure_runtime().pilot_service


def get_stream_controller() -> StreamControllerPort:
    return _ensure_runtime().stream_controller


def get_detector() -> DetectorPort | None:
    return _ensure_runtime().detector


def get_artifact_storage() -> ArtifactStorage | None:
    """Return artifact storage for resolving S3 image URIs."""
    return _ensure_runtime().artifact_storage


def get_auto_mission_service() -> AutoMissionService | None:
    """Return :class:`AutoMissionService`, if automatic mode is wired in."""
    return _ensure_runtime().auto_mission_service


def reset_state() -> None:
    """Reset mutable runtime state used by tests and local sessions."""
    if _STATE.runtime is None:
        return
    _STATE.runtime.reset_hook()
    _STATE.runtime.pilot_service.reset_runtime_state()
    _STATE.runtime = None


__all__ = [
    "ApiRuntime",
    "DetectorPort",
    "StreamControllerPort",
    "get_artifact_storage",
    "get_auto_mission_service",
    "get_container",
    "get_detector",
    "get_pilot_service",
    "get_stream_controller",
    "reset_state",
    "set_runtime",
]
