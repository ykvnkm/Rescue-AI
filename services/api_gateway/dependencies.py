from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

from config import config
from libs.core.application.contracts import (
    AlertRepository,
    FrameEventRepository,
    MissionRepository,
)
from libs.core.application.models import AlertRuleConfig
from libs.core.application.pilot_service import PilotService
from libs.infra.postgres import (
    EpisodeProjectionSettings,
    PostgresAlertRepository,
    PostgresDatabase,
    PostgresFrameEventRepository,
    PostgresMissionRepository,
)
from services.api_gateway.infrastructure import (
    DetectionStreamController,
    build_artifact_storage,
    load_alert_rules_and_metadata,
)
from services.api_gateway.infrastructure.memory_store import (
    InMemoryAlertRepository,
    InMemoryDatabase,
    InMemoryFrameEventRepository,
    InMemoryMissionRepository,
)


@dataclass
class AppContainer:
    """Process-level dependencies for api_gateway runtime."""

    pilot_service: PilotService
    stream_controller: DetectionStreamController
    reset_hook: Callable[[], None]


@lru_cache(maxsize=1)
def get_container() -> AppContainer:
    alert_rules, report_metadata = load_alert_rules_and_metadata()
    mission_repository, alert_repository, frame_repository, reset_hook = (
        _build_repositories(alert_rules=alert_rules)
    )
    artifact_storage = build_artifact_storage()

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

    return AppContainer(
        pilot_service=pilot_service,
        stream_controller=DetectionStreamController(),
        reset_hook=reset_hook,
    )


def get_pilot_service() -> PilotService:
    return get_container().pilot_service


def get_stream_controller() -> DetectionStreamController:
    return get_container().stream_controller


def reset_state() -> None:
    container = get_container()
    container.reset_hook()
    container.pilot_service.reset_runtime_state()
    get_container.cache_clear()


def _build_repositories(
    *,
    alert_rules: AlertRuleConfig,
) -> tuple[
    MissionRepository,
    AlertRepository,
    FrameEventRepository,
    Callable[[], None],
]:
    backend = config.get_non_empty("APP_REPOSITORY_BACKEND", default="memory").lower()
    if backend == "memory":
        db = InMemoryDatabase()
        return (
            InMemoryMissionRepository(db),
            InMemoryAlertRepository(db),
            InMemoryFrameEventRepository(db),
            lambda: _reset_memory_database(db),
        )
    if backend == "postgres":
        dsn = config.get_non_empty("APP_POSTGRES_DSN")
        if not dsn:
            raise ValueError("APP_POSTGRES_DSN is required for postgres backend")

        db = PostgresDatabase(dsn=dsn)
        episode_settings = EpisodeProjectionSettings(
            gt_gap_end_sec=alert_rules.gt_gap_end_sec,
            match_tolerance_sec=alert_rules.match_tolerance_sec,
        )
        return (
            PostgresMissionRepository(db),
            PostgresAlertRepository(db, episode_settings=episode_settings),
            PostgresFrameEventRepository(db, episode_settings=episode_settings),
            db.truncate_all,
        )
    raise ValueError(
        "APP_REPOSITORY_BACKEND must be one of: memory, postgres"
    )


def _reset_memory_database(db: InMemoryDatabase) -> None:
    db.missions.clear()
    db.alerts.clear()
    db.mission_frames.clear()
