"""Online API server entry point."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable

import uvicorn

from rescue_ai.application.pilot_service import PilotService
from rescue_ai.config import Settings, get_settings
from rescue_ai.domain.ports import (
    AlertRepository,
    FrameEventRepository,
    MissionRepository,
)
from rescue_ai.domain.value_objects import AlertRuleConfig
from rescue_ai.infrastructure.artifact_storage import build_s3_storage
from rescue_ai.infrastructure.contract_loader import load_alert_rules_and_metadata
from rescue_ai.infrastructure.postgres_connection import (
    dsn_with_search_path,
    wait_for_postgres,
)
from rescue_ai.infrastructure.rpi_client import RpiClient
from rescue_ai.interfaces.api.dependencies import ApiRuntime, set_runtime


@dataclass
class RpiStreamState:
    """RPi streaming session state bound to one mission."""

    mission_id: str
    rpi_mission_id: str
    session_id: str
    rtsp_url: str
    target_fps: float
    running: bool
    started_at: str
    last_stats: dict[str, object] | None = None
    error: str | None = None


class DetectionStreamController:
    """Controls Raspberry Pi streaming sessions for mission runs."""

    def __init__(self, settings: Settings) -> None:
        self._rpi_settings = settings.rpi
        self._sessions: dict[str, RpiStreamState] = {}

    def start(
        self,
        *,
        mission_id: str,
        rpi_mission_id: str,
        target_fps: float,
    ) -> RpiStreamState:
        current = self._sessions.get(mission_id)
        if current is not None and current.running:
            raise ValueError("Stream already running for mission")

        session = self._client().start_stream(
            mission_id=rpi_mission_id,
            target_fps=target_fps,
            timeout_sec=self._rpi_settings.timeout_sec,
        )
        state = RpiStreamState(
            mission_id=mission_id,
            rpi_mission_id=rpi_mission_id,
            session_id=session.session_id,
            rtsp_url=session.rtsp_url,
            target_fps=target_fps,
            running=True,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        self._sessions[mission_id] = state
        return state

    def stop(self, mission_id: str) -> RpiStreamState | None:
        state = self._sessions.get(mission_id)
        if state is None:
            return None

        if state.running:
            try:
                self._client().stop_stream(
                    state.session_id, timeout_sec=self._rpi_settings.timeout_sec
                )
            except (ValueError, RuntimeError, OSError) as error:
                state.error = f"{type(error).__name__}: {error}"

        state.running = False
        return state

    def get_state(self, mission_id: str) -> RpiStreamState | None:
        state = self._sessions.get(mission_id)
        if state is None:
            return None
        if not state.running:
            return state

        try:
            stats = self._client().session_stats(
                state.session_id, timeout_sec=self._rpi_settings.timeout_sec
            )
            state.last_stats = stats
            state.error = None
        except (ValueError, RuntimeError, OSError) as error:
            state.error = f"{type(error).__name__}: {error}"
        return state

    def as_payload(self, mission_id: str) -> dict[str, object] | None:
        state = self.get_state(mission_id)
        if state is None:
            return None
        return asdict(state)

    def check_rpi_health(self) -> dict[str, object]:
        return self._client().health(timeout_sec=self._rpi_settings.timeout_sec)

    def list_rpi_missions(self) -> list[dict[str, str]]:
        catalog = self._client().catalog(timeout_sec=self._rpi_settings.timeout_sec)
        return [
            {"mission_id": mission.mission_id, "name": mission.name}
            for mission in catalog.missions
        ]

    def _client(self) -> RpiClient:
        return RpiClient(self._rpi_settings)


def build_api_runtime() -> (
    tuple[PilotService, DetectionStreamController, Callable[[], None]]
):
    """Assemble API runtime dependencies (composition root)."""
    settings = get_settings()
    alert_rules, report_metadata = load_alert_rules_and_metadata(
        service_version=settings.app.service_version
    )
    mission_repository, alert_repository, frame_repository, reset_hook = (
        _build_repositories(
            alert_rules=alert_rules,
            settings=settings,
        )
    )

    artifact_storage = build_s3_storage(settings.storage)

    pilot_service = PilotService(
        dependencies=PilotService.Dependencies(
            mission_repository=mission_repository,
            alert_repository=alert_repository,
            frame_event_repository=frame_repository,
            artifact_storage=artifact_storage,
        ),
        alert_rules=alert_rules,
    )
    pilot_service.set_report_metadata(report_metadata)

    stream_controller = DetectionStreamController(settings=settings)
    return pilot_service, stream_controller, reset_hook


def main() -> None:
    """Start the API server and initialize runtime dependencies."""
    settings = get_settings()
    _prepare_postgres_backend()
    pilot_service, stream_controller, reset_hook = build_api_runtime()
    set_runtime(
        ApiRuntime(
            pilot_service=pilot_service,
            stream_controller=stream_controller,
            reset_hook=reset_hook,
        )
    )
    uvicorn.run(
        "rescue_ai.interfaces.api.app:app",
        host=settings.api.host,
        port=settings.api.port,
    )


def _prepare_postgres_backend() -> None:
    settings = get_settings()
    dsn = settings.database.dsn
    if not dsn:
        raise RuntimeError("DB_DSN is required")
    app_dsn = dsn_with_search_path(dsn, "app")
    wait_for_postgres(app_dsn, timeout_sec=settings.api.postgres_ready_timeout_sec)


def _build_repositories(
    *,
    alert_rules: AlertRuleConfig,
    settings: Settings,
) -> tuple[
    MissionRepository,
    AlertRepository,
    FrameEventRepository,
    Callable[[], None],
]:
    from rescue_ai.infrastructure.postgres_connection import PostgresDatabase
    from rescue_ai.infrastructure.postgres_repositories import (
        EpisodeProjectionSettings,
        PostgresAlertRepository,
        PostgresFrameEventRepository,
        PostgresMissionRepository,
    )

    dsn = settings.database.dsn.strip()
    if not dsn:
        raise ValueError("DB_DSN is required")
    app_dsn = dsn_with_search_path(dsn, "app")

    postgres_db = PostgresDatabase(dsn=app_dsn)
    episode_settings = EpisodeProjectionSettings(
        gt_gap_end_sec=alert_rules.gt_gap_end_sec,
        match_tolerance_sec=alert_rules.match_tolerance_sec,
    )
    return (
        PostgresMissionRepository(postgres_db),
        PostgresAlertRepository(postgres_db, episode_settings=episode_settings),
        PostgresFrameEventRepository(postgres_db, episode_settings=episode_settings),
        postgres_db.truncate_all,
    )


if __name__ == "__main__":
    main()
