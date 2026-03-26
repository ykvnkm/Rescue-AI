"""Pilot mission engine adapter over PilotService."""

from __future__ import annotations

from rescue_ai.application.pilot_service import PilotService
from rescue_ai.domain.entities import Alert, AlertRuleConfig, Detection, FrameEvent
from rescue_ai.infrastructure.memory_repositories import (
    InMemoryAlertRepository,
    InMemoryArtifactStorage,
    InMemoryDatabase,
    InMemoryFrameEventRepository,
    InMemoryMissionRepository,
)


class PilotMissionEngine:
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
        detections: list[Detection],
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
        """Forward an auto-review decision to the pilot service."""
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


class PilotMissionEngineFactory:
    """Creates isolated in-memory pilot engine instances per run."""

    def create(
        self,
        alert_rules: AlertRuleConfig,
        report_metadata: dict[str, object],
    ) -> PilotMissionEngine:
        db = InMemoryDatabase()
        pilot = PilotService(
            dependencies=PilotService.Dependencies(
                mission_repository=InMemoryMissionRepository(db),
                alert_repository=InMemoryAlertRepository(db),
                frame_event_repository=InMemoryFrameEventRepository(db),
                artifact_storage=InMemoryArtifactStorage(),
            ),
            alert_rules=alert_rules,
        )
        pilot.set_report_metadata(report_metadata)
        return PilotMissionEngine(pilot=pilot)

    def factory_name(self) -> str:
        return "pilot-in-memory"
