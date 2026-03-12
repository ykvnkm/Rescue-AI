from __future__ import annotations

from libs.batch.application.ports import MissionEngineFactoryPort, MissionEnginePort
from libs.batch.infrastructure.in_memory_repositories import (
    InMemoryAlertRepo,
    InMemoryBatchDb,
    InMemoryFrameEventRepo,
    InMemoryMissionRepo,
)
from libs.core.application.models import AlertRuleConfig, DetectionInput
from libs.core.application.pilot_service import PilotService
from libs.core.domain.entities import Alert, FrameEvent


class PilotMissionEngine(MissionEnginePort):
    """Mission-engine adapter over core `PilotService`."""

    def __init__(self, pilot: PilotService) -> None:
        self._pilot = pilot

    def create_and_start_mission(
        self,
        source_name: str,
        total_frames: int,
        fps: float,
        report_metadata: dict[str, object],
    ) -> str:
        self._pilot.set_report_metadata(report_metadata)
        mission = self._pilot.create_mission(
            source_name=source_name,
            total_frames=total_frames,
            fps=fps,
        )
        started = self._pilot.start_mission(mission.mission_id)
        if started is None:
            raise ValueError("Failed to start mission")
        return mission.mission_id

    def ingest_frame(
        self,
        mission_id: str,
        frame_event: FrameEvent,
        detections: list[DetectionInput],
    ) -> list[Alert]:
        if frame_event.mission_id != mission_id:
            raise ValueError("Mission id mismatch")
        return self._pilot.ingest_frame_event(
            frame_event=frame_event, detections=detections
        )

    def review_alert(
        self,
        alert_id: str,
        status: str,
        reviewed_at_sec: float,
        reason: str,
    ) -> None:
        result = self._pilot.review_alert(
            alert_id,
            {
                "status": status,
                "reviewed_by": "batch-auto-review",
                "reviewed_at_sec": reviewed_at_sec,
                "decision_reason": reason,
            },
        )
        if result is None:
            raise ValueError("Alert not found")

    def complete(self, mission_id: str, completed_frame_id: int | None) -> None:
        result = self._pilot.complete_mission(
            mission_id=mission_id,
            completed_frame_id=completed_frame_id,
        )
        if result is None:
            raise ValueError("Mission not found")

    def build_report(self, mission_id: str) -> dict[str, object]:
        return self._pilot.get_mission_report(mission_id)


class PilotMissionEngineFactory(MissionEngineFactoryPort):
    """Creates isolated in-memory pilot engine instances per run."""

    def create(
        self,
        alert_rules: AlertRuleConfig,
        report_metadata: dict[str, object],
    ) -> MissionEnginePort:
        db = InMemoryBatchDb()
        pilot = PilotService(
            mission_repository=InMemoryMissionRepo(db),
            alert_repository=InMemoryAlertRepo(db),
            frame_event_repository=InMemoryFrameEventRepo(db),
            alert_rules=alert_rules,
        )
        pilot.set_report_metadata(report_metadata)
        return PilotMissionEngine(pilot=pilot)

    def factory_name(self) -> str:
        return "pilot-in-memory"
