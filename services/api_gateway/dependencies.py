from libs.core.application.pilot_service import PilotService
from services.api_gateway.infrastructure.memory_store import (
    InMemoryAlertRepository,
    InMemoryDatabase,
    InMemoryFrameEventRepository,
    InMemoryMissionRepository,
)

db = InMemoryDatabase()
mission_repository = InMemoryMissionRepository(db)
alert_repository = InMemoryAlertRepository(db)
frame_repository = InMemoryFrameEventRepository(db)
pilot_service = PilotService(
    mission_repository=mission_repository,
    alert_repository=alert_repository,
    frame_event_repository=frame_repository,
)


def get_pilot_service() -> PilotService:
    return pilot_service


def reset_state() -> None:
    db.missions.clear()
    db.alerts.clear()
    db.mission_frames.clear()
