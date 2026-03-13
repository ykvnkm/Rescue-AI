from __future__ import annotations

from libs.batch.infrastructure.in_memory_artifact_storage import InMemoryArtifactStorage
from libs.batch.infrastructure.in_memory_repositories import (
    InMemoryAlertRepo,
    InMemoryBatchDb,
    InMemoryFrameEventRepo,
    InMemoryMissionRepo,
)
from libs.core.application.models import DetectionInput
from libs.core.application.pilot_service import PilotService
from libs.core.domain.entities import FrameEvent


class ArtifactStorageDouble(InMemoryArtifactStorage):
    """Artifact storage test double that rewrites stored frame URIs."""

    def __init__(self, stored_frame_uri: str = "memory://missions/m-1/frames/1.jpg"):
        super().__init__()
        self.stored_frame_uri = stored_frame_uri

    def store_frame(self, mission_id: str, frame_id: int, source_uri: str) -> str:
        _ = (mission_id, frame_id, source_uri)
        return self.stored_frame_uri


def _build_service(
    artifact_storage: InMemoryArtifactStorage | None = None,
) -> tuple[PilotService, InMemoryBatchDb]:
    db = InMemoryBatchDb()
    service = PilotService(
        dependencies=PilotService.Dependencies(
            mission_repository=InMemoryMissionRepo(db),
            alert_repository=InMemoryAlertRepo(db),
            frame_event_repository=InMemoryFrameEventRepo(db),
            artifact_storage=artifact_storage or InMemoryArtifactStorage(),
        )
    )
    return service, db


def test_update_mission_persists_total_frames() -> None:
    service, _ = _build_service()
    mission = service.create_mission(source_name="pilot", total_frames=0, fps=2.0)

    updated = service.update_mission(mission.mission_id, total_frames=12)

    assert updated is not None
    assert updated.total_frames == 12
    reloaded = service.get_mission(mission.mission_id)
    assert reloaded is not None
    assert reloaded.total_frames == 12


def test_ingest_frame_event_persists_stored_image_uri_for_frame_and_alert() -> None:
    artifacts = ArtifactStorageDouble()
    service, db = _build_service(artifact_storage=artifacts)
    mission = service.create_mission(source_name="pilot", total_frames=1, fps=2.0)
    started = service.start_mission(mission.mission_id)
    assert started is not None

    alerts = service.ingest_frame_event(
        frame_event=FrameEvent(
            mission_id=mission.mission_id,
            frame_id=1,
            ts_sec=0.0,
            image_uri="file:///tmp/frame.jpg",
            gt_person_present=True,
            gt_episode_id="ep-1",
        ),
        detections=[
            DetectionInput(
                bbox=(10.0, 20.0, 30.0, 40.0),
                score=0.99,
                label="person",
                model_name="yolo8n",
                explanation="strong-hit",
            )
        ],
    )

    assert len(alerts) == 1
    assert alerts[0].image_uri == artifacts.stored_frame_uri
    assert db.frames[mission.mission_id][0].image_uri == artifacts.stored_frame_uri
    assert db.alerts[alerts[0].alert_id].image_uri == artifacts.stored_frame_uri
