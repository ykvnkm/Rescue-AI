from __future__ import annotations

from rescue_ai.application.pilot_service import PilotService
from rescue_ai.domain.entities import Detection, FrameEvent
from rescue_ai.infrastructure.memory_repositories import (
    InMemoryAlertRepository,
    InMemoryArtifactStorage,
    InMemoryDatabase,
    InMemoryFrameEventRepository,
    InMemoryMissionRepository,
)


def _build_pilot_service(
    artifact_storage: InMemoryArtifactStorage | None = None,
) -> tuple[PilotService, InMemoryDatabase]:
    db = InMemoryDatabase()
    service = PilotService(
        dependencies=PilotService.Dependencies(
            mission_repository=InMemoryMissionRepository(db),
            alert_repository=InMemoryAlertRepository(db),
            frame_event_repository=InMemoryFrameEventRepository(db),
            artifact_storage=artifact_storage or InMemoryArtifactStorage(),
        )
    )
    return service, db


def test_update_mission_persists_total_frames() -> None:
    service, _ = _build_pilot_service()
    mission = service.create_mission(source_name="pilot", total_frames=0, fps=2.0)

    updated = service.update_mission(mission.mission_id, total_frames=12)

    assert updated is not None
    assert updated.total_frames == 12
    reloaded = service.get_mission(mission.mission_id)
    assert reloaded is not None
    assert reloaded.total_frames == 12


def test_ingest_frame_event_persists_stored_image_uri_for_frame_and_alert() -> None:
    artifacts = InMemoryArtifactStorage()
    service, db = _build_pilot_service(artifact_storage=artifacts)
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
            Detection(
                bbox=(10.0, 20.0, 30.0, 40.0),
                score=0.99,
                label="person",
                model_name="yolo8n",
                explanation="strong-hit",
            )
        ],
    )

    expected_uri = f"memory://missions/{mission.mission_id}/frames/1.jpg"
    assert len(alerts) == 1
    assert alerts[0].image_uri == expected_uri
    assert db.mission_frames[mission.mission_id][0].image_uri == expected_uri
    assert db.alerts[alerts[0].alert_id].image_uri == expected_uri
    assert artifacts.stored_frames[(mission.mission_id, 1)] == expected_uri


def test_review_alert_cannot_be_applied_twice() -> None:
    service, _ = _build_pilot_service()
    mission = service.create_mission(source_name="pilot", total_frames=1, fps=2.0)
    service.start_mission(mission.mission_id)

    alert = service.ingest_frame_event(
        frame_event=FrameEvent(
            mission_id=mission.mission_id,
            frame_id=1,
            ts_sec=0.0,
            image_uri="file:///tmp/frame.jpg",
            gt_person_present=True,
            gt_episode_id="ep-1",
        ),
        detections=[
            Detection(
                bbox=(10.0, 20.0, 30.0, 40.0),
                score=0.99,
                label="person",
                model_name="yolo8n",
                explanation="strong-hit",
            )
        ],
    )[0]

    reviewed = service.review_alert(
        alert.alert_id,
        status="reviewed_confirmed",
        reviewed_by="operator-1",
        reviewed_at_sec=None,
        decision_reason="valid target",
    )

    assert reviewed is not None
    assert reviewed.reviewed_at_sec == alert.ts_sec

    try:
        service.review_alert(
            alert.alert_id,
            status="reviewed_rejected",
            reviewed_by="operator-2",
            reviewed_at_sec=1.0,
            decision_reason="should fail",
        )
    except ValueError as error:
        assert str(error) == "Alert already reviewed"
    else:  # pragma: no cover
        raise AssertionError("Expected repeated review to be rejected")
