from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from libs.core.application.pilot_service import PilotService
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

    db: InMemoryDatabase
    pilot_service: PilotService
    stream_controller: DetectionStreamController


@lru_cache(maxsize=1)
def get_container() -> AppContainer:
    db = InMemoryDatabase()
    mission_repository = InMemoryMissionRepository(db)
    alert_repository = InMemoryAlertRepository(db)
    frame_repository = InMemoryFrameEventRepository(db)
    artifact_storage = build_artifact_storage()
    alert_rules, report_metadata = load_alert_rules_and_metadata()

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
        db=db,
        pilot_service=pilot_service,
        stream_controller=DetectionStreamController(),
    )


def get_pilot_service() -> PilotService:
    return get_container().pilot_service


def get_stream_controller() -> DetectionStreamController:
    return get_container().stream_controller


def reset_state() -> None:
    container = get_container()
    container.db.missions.clear()
    container.db.alerts.clear()
    container.db.mission_frames.clear()
    container.pilot_service.reset_runtime_state()
