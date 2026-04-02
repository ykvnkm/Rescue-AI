from __future__ import annotations

from rescue_ai.application.pilot_service import PilotService
from rescue_ai.domain.entities import Detection, FrameEvent
from rescue_ai.domain.ports import AlertReviewPayload
from rescue_ai.domain.value_objects import AlertRuleConfig, AlertStatus
from tests.support.in_memory_repositories import (
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
    alert_rules = AlertRuleConfig(
        score_threshold=0.2,
        window_sec=1.0,
        quorum_k=1,
        cooldown_sec=1.5,
        gap_end_sec=1.2,
        gt_gap_end_sec=1.0,
        match_tolerance_sec=1.2,
    )
    service = PilotService(
        dependencies=PilotService.Dependencies(
            mission_repository=InMemoryMissionRepository(db),
            alert_repository=InMemoryAlertRepository(db),
            frame_event_repository=InMemoryFrameEventRepository(db),
            artifact_storage=artifact_storage or InMemoryArtifactStorage(),
        ),
        alert_rules=alert_rules,
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


def test_ingest_frame_event_without_alert_skips_frame_upload() -> None:
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
            gt_person_present=False,
            gt_episode_id=None,
        ),
        detections=[],
    )

    assert not alerts
    assert not artifacts.stored_frames
    assert db.mission_frames[mission.mission_id][0].image_uri == "file:///tmp/frame.jpg"


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

    review: AlertReviewPayload = {
        "status": AlertStatus.REVIEWED_CONFIRMED,
        "reviewed_by": "operator-1",
        "reviewed_at_sec": None,
        "decision_reason": "valid target",
    }
    reviewed = service.review_alert(alert.alert_id, review)

    assert reviewed is not None
    assert reviewed.reviewed_at_sec == alert.ts_sec

    second_review: AlertReviewPayload = {
        "status": AlertStatus.REVIEWED_REJECTED,
        "reviewed_by": "operator-2",
        "reviewed_at_sec": 1.0,
        "decision_reason": "should fail",
    }
    try:
        service.review_alert(alert.alert_id, second_review)
    except ValueError as error:
        assert str(error) == "Alert already reviewed"
    else:  # pragma: no cover
        raise AssertionError("Expected repeated review to be rejected")


def test_review_alert_same_status_is_idempotent() -> None:
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

    review: AlertReviewPayload = {
        "status": AlertStatus.REVIEWED_CONFIRMED,
        "reviewed_by": "operator-1",
        "reviewed_at_sec": 1.0,
        "decision_reason": "valid target",
    }
    first = service.review_alert(alert.alert_id, review)
    second = service.review_alert(alert.alert_id, review)

    assert first is not None
    assert second is not None
    assert second.status == AlertStatus.REVIEWED_CONFIRMED


def test_mission_report_marks_gt_kpis_not_applicable_without_gt() -> None:
    service, _ = _build_pilot_service()
    mission = service.create_mission(source_name="pilot", total_frames=1, fps=2.0)
    service.start_mission(mission.mission_id)
    service.ingest_frame_event(
        frame_event=FrameEvent(
            mission_id=mission.mission_id,
            frame_id=1,
            ts_sec=0.0,
            image_uri="file:///tmp/frame.jpg",
            gt_person_present=False,
            gt_episode_id=None,
        ),
        detections=[],
    )
    service.complete_mission(mission.mission_id, completed_frame_id=1)

    report = service.get_mission_report(mission.mission_id)

    assert report["gt_available"] is False
    assert report["episodes_total"] is None
    assert report["episodes_found"] is None
    assert report["recall_event"] is None
    assert report["ttfc_sec"] is None
    assert report["false_alerts_total"] is None
    assert report["fp_per_minute"] is None


def test_create_mission_is_idempotent_for_same_source() -> None:
    service, db = _build_pilot_service()

    first = service.create_mission(
        source_name="rpi:mission-1", total_frames=10, fps=6.0
    )
    second = service.create_mission(
        source_name="rpi:mission-1",
        total_frames=25,
        fps=8.0,
    )

    assert first.mission_id == second.mission_id
    assert len(db.missions) == 1
    assert second.total_frames == 25
    assert second.fps == 8.0


def test_reingest_same_frame_keeps_single_alert_and_frame_event() -> None:
    service, db = _build_pilot_service()
    mission = service.create_mission(
        source_name="rpi:mission-1", total_frames=1, fps=6.0
    )
    service.start_mission(mission.mission_id)

    frame = FrameEvent(
        mission_id=mission.mission_id,
        frame_id=1,
        ts_sec=0.0,
        image_uri="file:///tmp/frame.jpg",
        gt_person_present=True,
        gt_episode_id="ep-1",
    )
    detections = [
        Detection(
            bbox=(10.0, 20.0, 30.0, 40.0),
            score=0.99,
            label="person",
            model_name="yolo8n",
            explanation="strong-hit",
        )
    ]

    first_alerts = service.ingest_frame_event(frame_event=frame, detections=detections)
    second_alerts = service.ingest_frame_event(frame_event=frame, detections=detections)

    assert len(first_alerts) == 1
    assert not second_alerts
    assert len(db.alerts) == 1
    assert len(db.mission_frames[mission.mission_id]) == 1
