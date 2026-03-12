from libs.core.application.pilot_service import PilotService
from services.api_gateway.infrastructure import (
    DetectionStreamController,
    load_alert_rules_and_metadata,
)
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
alert_rules, report_metadata = load_alert_rules_and_metadata()
pilot_service = PilotService(
    mission_repository=mission_repository,
    alert_repository=alert_repository,
    frame_event_repository=frame_repository,
    alert_rules=alert_rules,
)
pilot_service.set_report_metadata(report_metadata)
stream_controller = DetectionStreamController()


def get_pilot_service() -> PilotService:
    return pilot_service


def get_stream_controller() -> DetectionStreamController:
    return stream_controller


def reset_state() -> None:
    db.missions.clear()
    db.alerts.clear()
    db.mission_frames.clear()
    pilot_service.reset_runtime_state()
