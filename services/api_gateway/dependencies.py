from libs.core.application.models import AlertRuleConfig
from libs.core.application.pilot_service import PilotService
from services.api_gateway.infrastructure.artifact_storage import build_artifact_storage
from services.api_gateway.infrastructure.memory_store import (
    InMemoryAlertRepository,
    InMemoryDatabase,
    InMemoryFrameEventRepository,
    InMemoryMissionRepository,
)
from services.detection_service.infrastructure.runtime_contract import (
    load_stream_contract,
)

db = InMemoryDatabase()
mission_repository = InMemoryMissionRepository(db)
alert_repository = InMemoryAlertRepository(db)
frame_repository = InMemoryFrameEventRepository(db)
artifact_storage = build_artifact_storage()

stream_contract = load_stream_contract()

alert_rules = AlertRuleConfig(
    score_threshold=stream_contract.alert_rules.score_threshold,
    window_sec=stream_contract.alert_rules.window_sec,
    quorum_k=stream_contract.alert_rules.quorum_k,
    cooldown_sec=stream_contract.alert_rules.cooldown_sec,
    gap_end_sec=stream_contract.alert_rules.gap_end_sec,
    gt_gap_end_sec=stream_contract.alert_rules.gt_gap_end_sec,
    match_tolerance_sec=stream_contract.alert_rules.match_tolerance_sec,
)

pilot_service = PilotService(
    mission_repository=mission_repository,
    alert_repository=alert_repository,
    frame_event_repository=frame_repository,
    artifact_storage=artifact_storage,
    alert_rules=alert_rules,
)

pilot_service.set_report_metadata(
    {
        "config_name": stream_contract.report_provenance.config_name,
        "config_hash": stream_contract.report_provenance.config_hash,
        "config_path": stream_contract.report_provenance.config_path,
        "model_url": stream_contract.inference.model_url,
        "service_version": stream_contract.report_provenance.service_version,
    }
)


def get_pilot_service() -> PilotService:
    return pilot_service


def reset_state() -> None:
    db.missions.clear()
    db.alerts.clear()
    db.mission_frames.clear()
    pilot_service.reset_runtime_state()
