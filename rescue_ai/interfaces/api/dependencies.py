"""FastAPI dependency providers backed by runtime state.

No infrastructure wiring lives in this module.
Runtime dependencies are injected by the entrypoint and lazily
initialized as a fallback for local tests.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Callable, Protocol

from rescue_ai.application.pilot_service import PilotService


class StreamControllerPort(Protocol):
    """Minimal stream controller contract consumed by API routes."""

    def start(
        self,
        *,
        mission_id: str,
        rpi_mission_id: str,
        target_fps: float,
    ) -> object: ...

    def stop(self, mission_id: str) -> object | None: ...

    def as_payload(self, mission_id: str) -> dict[str, object] | None: ...

    def check_rpi_health(self) -> dict[str, object]: ...

    def list_rpi_missions(self) -> list[dict[str, str]]: ...


@dataclass
class ApiRuntime:
    """Runtime dependency bundle for API route handlers."""

    pilot_service: PilotService
    stream_controller: StreamControllerPort
    reset_hook: Callable[[], None]


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
        pilot_service, stream_controller, reset_hook = build_api_runtime()
        _STATE.runtime = ApiRuntime(
            pilot_service=pilot_service,
            stream_controller=stream_controller,
            reset_hook=reset_hook,
        )
    return _STATE.runtime


def _clear_runtime() -> None:
    _STATE.runtime = None


def get_container() -> ApiRuntime:
    """Return initialized API runtime container."""
    return _ensure_runtime()


get_container.cache_clear = _clear_runtime  # type: ignore[attr-defined]


def get_pilot_service() -> PilotService:
    return _ensure_runtime().pilot_service


def get_stream_controller() -> StreamControllerPort:
    return _ensure_runtime().stream_controller


def reset_state() -> None:
    """Reset mutable runtime state used by tests and local sessions."""
    if _STATE.runtime is None:
        return
    _STATE.runtime.reset_hook()
    _STATE.runtime.pilot_service.reset_runtime_state()
    _STATE.runtime = None


__all__ = [
    "ApiRuntime",
    "StreamControllerPort",
    "get_container",
    "get_pilot_service",
    "get_stream_controller",
    "reset_state",
    "set_runtime",
]
