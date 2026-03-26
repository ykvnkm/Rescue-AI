"""FastAPI dependency providers backed by runtime state.

No infrastructure wiring lives in this module.
Runtime dependencies are injected by the entrypoint and lazily
initialized as a fallback for local tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from rescue_ai.application.pilot_service import PilotService
from rescue_ai.application.stream_orchestrator import StreamConfig, StreamState


class StreamControllerPort(Protocol):
    """Minimal stream controller contract consumed by API routes."""

    def build_config(self, mission_id: str, options: Any) -> StreamConfig: ...
    def start(self, config: StreamConfig) -> StreamState: ...
    def stop(self, mission_id: str) -> StreamState | None: ...
    def wait_stopped(
        self, mission_id: str, timeout_sec: float = 3.0
    ) -> StreamState | None: ...
    def get_state(self, mission_id: str) -> StreamState | None: ...


@dataclass
class ApiRuntime:
    """Runtime dependency bundle for API route handlers."""

    pilot_service: PilotService
    stream_controller: StreamControllerPort
    reset_hook: Callable[[], None]


_runtime: ApiRuntime | None = None


def set_runtime(runtime: ApiRuntime) -> None:
    """Install runtime dependencies for API requests."""
    global _runtime
    _runtime = runtime


def _ensure_runtime() -> ApiRuntime:
    global _runtime
    if _runtime is None:
        from rescue_ai.interfaces.cli.online import build_api_runtime

        pilot_service, stream_controller, reset_hook = build_api_runtime()
        _runtime = ApiRuntime(
            pilot_service=pilot_service,
            stream_controller=stream_controller,
            reset_hook=reset_hook,
        )
    return _runtime


def _clear_runtime() -> None:
    global _runtime
    _runtime = None


def get_container() -> ApiRuntime:
    """Backward-compatible alias for tests expecting a cached container getter."""
    return _ensure_runtime()


get_container.cache_clear = _clear_runtime  # type: ignore[attr-defined]


def get_pilot_service() -> PilotService:
    return _ensure_runtime().pilot_service


def get_stream_controller() -> StreamControllerPort:
    return _ensure_runtime().stream_controller


def reset_state() -> None:
    """Reset mutable runtime state used by tests and local sessions."""
    global _runtime
    if _runtime is None:
        return
    _runtime.reset_hook()
    _runtime.pilot_service.reset_runtime_state()
    _runtime = None


__all__ = [
    "ApiRuntime",
    "StreamControllerPort",
    "get_container",
    "get_pilot_service",
    "get_stream_controller",
    "reset_state",
    "set_runtime",
]
