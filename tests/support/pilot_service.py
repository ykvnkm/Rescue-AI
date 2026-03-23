from __future__ import annotations

from dataclasses import dataclass, field

from libs.core.application.contracts import ArtifactBlob
from libs.core.application.pilot_service import PilotService
from libs.infra.memory import (
    InMemoryAlertRepository,
    InMemoryDatabase,
    InMemoryFrameEventRepository,
    InMemoryMissionRepository,
)


@dataclass
class InMemoryArtifactStorageDouble:
    """Minimal artifact storage double used by PilotService tests."""

    stored_frame_uri: str = "memory://missions/m-1/frames/1.jpg"
    stored_frames: dict[tuple[str, int], str] = field(default_factory=dict)
    reports: dict[str, dict[str, object]] = field(default_factory=dict)

    def store_frame(self, mission_id: str, frame_id: int, source_uri: str) -> str:
        _ = source_uri
        stored_uri = self.stored_frame_uri.format(
            mission_id=mission_id,
            frame_id=frame_id,
        )
        self.stored_frames[(mission_id, frame_id)] = stored_uri
        return stored_uri

    def load_frame(self, image_uri: str) -> ArtifactBlob | None:
        if image_uri in self.stored_frames.values():
            return ArtifactBlob(
                content=b"",
                media_type="image/jpeg",
                filename=image_uri.split("/")[-1] or "frame.jpg",
            )
        return None

    def save_mission_report(self, mission_id: str, report: dict[str, object]) -> str:
        self.reports[mission_id] = dict(report)
        return f"memory://missions/{mission_id}/report.json"

    def load_mission_report(self, mission_id: str) -> dict[str, object] | None:
        payload = self.reports.get(mission_id)
        return None if payload is None else dict(payload)


def build_pilot_service(
    artifact_storage: InMemoryArtifactStorageDouble | None = None,
) -> tuple[PilotService, InMemoryDatabase]:
    db = InMemoryDatabase()
    service = PilotService(
        dependencies=PilotService.Dependencies(
            mission_repository=InMemoryMissionRepository(db),
            alert_repository=InMemoryAlertRepository(db),
            frame_event_repository=InMemoryFrameEventRepository(db),
            artifact_storage=artifact_storage or InMemoryArtifactStorageDouble(),
        )
    )
    return service, db
