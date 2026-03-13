from __future__ import annotations

import os
from typing import Any

import pytest

from libs.batch.infrastructure.in_memory_artifact_storage import InMemoryArtifactStorage
from libs.core.application.models import DetectionInput
from libs.core.application.pilot_service import PilotService
from libs.core.domain.entities import (
    Alert,
    AlertEvidence,
    AlertLifecycle,
    DetectionData,
    FrameEvent,
    Mission,
)
from libs.infra.postgres import (
    EpisodeProjectionSettings,
    PostgresAlertRepository,
    PostgresDatabase,
    PostgresFrameEventRepository,
    PostgresMissionRepository,
)


@pytest.fixture()
def postgres_runtime() -> dict[str, Any]:
    dsn = os.getenv("APP_TEST_POSTGRES_DSN") or os.getenv("APP_POSTGRES_DSN")
    if not dsn:
        pytest.skip("APP_TEST_POSTGRES_DSN or APP_POSTGRES_DSN is not set")

    db = PostgresDatabase(dsn=dsn)
    _ensure_test_schema(db)
    db.truncate_all()
    yield {
        "db": db,
        "mission_repo": PostgresMissionRepository(db),
        "alert_repo": PostgresAlertRepository(
            db,
            episode_settings=EpisodeProjectionSettings(
                gt_gap_end_sec=1.0,
                match_tolerance_sec=1.2,
            ),
        ),
        "frame_repo": PostgresFrameEventRepository(
            db,
            episode_settings=EpisodeProjectionSettings(
                gt_gap_end_sec=1.0,
                match_tolerance_sec=1.2,
            ),
        ),
    }
    db.truncate_all()


@pytest.mark.integration
def test_postgres_mission_repository_roundtrip(
    postgres_runtime: dict[str, Any],
) -> None:
    repo = postgres_runtime["mission_repo"]
    mission = Mission(
        mission_id="mission-1",
        source_name="dataset-a",
        status="created",
        created_at="2026-03-14T00:00:00+00:00",
        total_frames=10,
        fps=2.5,
    )

    repo.create(mission)

    stored = repo.get("mission-1")
    assert stored is not None
    assert stored.source_name == "dataset-a"
    assert stored.total_frames == 10

    updated = repo.update_details("mission-1", total_frames=42, fps=6.0)
    assert updated is not None
    assert updated.total_frames == 42
    assert updated.fps == 6.0

    completed = repo.update_status(
        "mission-1",
        status="completed",
        completed_frame_id=41,
    )
    assert completed is not None
    assert completed.status == "completed"
    assert completed.completed_frame_id == 41


@pytest.mark.integration
def test_postgres_alert_repository_roundtrip(
    postgres_runtime: dict[str, Any],
) -> None:
    mission_repo = postgres_runtime["mission_repo"]
    frame_repo = postgres_runtime["frame_repo"]
    alert_repo = postgres_runtime["alert_repo"]
    mission_repo.create(
        Mission(
            mission_id="mission-1",
            source_name="dataset-a",
            status="running",
            created_at="2026-03-14T00:00:00+00:00",
            total_frames=2,
            fps=2.0,
        )
    )
    frame_repo.add(
        FrameEvent(
            mission_id="mission-1",
            frame_id=1,
            ts_sec=0.5,
            image_uri="s3://bucket/frames/1.jpg",
            gt_person_present=True,
            gt_episode_id="ep-1",
        )
    )

    alert = Alert(
        alert_id="alert-1",
        mission_id="mission-1",
        frame_id=1,
        ts_sec=0.5,
        image_uri="s3://bucket/frames/1.jpg",
        evidence=AlertEvidence(
            people_detected=2,
            primary_detection=DetectionData(
                bbox=(10.0, 20.0, 30.0, 40.0),
                score=0.98,
                label="person",
                model_name="yolo8n",
                explanation="clear-target",
            ),
            detections=[
                DetectionData(
                    bbox=(10.0, 20.0, 30.0, 40.0),
                    score=0.98,
                    label="person",
                    model_name="yolo8n",
                    explanation="clear-target",
                )
            ],
        ),
        lifecycle=AlertLifecycle(status="queued"),
    )

    alert_repo.add(alert)

    stored = alert_repo.get("alert-1")
    assert stored is not None
    assert stored.evidence.people_detected == 2
    assert stored.evidence.primary_detection.bbox == (10.0, 20.0, 30.0, 40.0)

    listed = alert_repo.list(mission_id="mission-1", status="queued")
    assert [item.alert_id for item in listed] == ["alert-1"]

    reviewed = alert_repo.update_status(
        "alert-1",
        {
            "status": "reviewed_confirmed",
            "reviewed_by": "operator-1",
            "reviewed_at_sec": 0.9,
            "decision_reason": "valid target",
        },
    )
    assert reviewed is not None
    assert reviewed.lifecycle.status == "reviewed_confirmed"
    assert reviewed.lifecycle.reviewed_by == "operator-1"


@pytest.mark.integration
def test_postgres_frame_event_repository_lists_by_mission(
    postgres_runtime: dict[str, Any],
) -> None:
    mission_repo = postgres_runtime["mission_repo"]
    frame_repo = postgres_runtime["frame_repo"]
    mission_repo.create(
        Mission(
            mission_id="mission-1",
            source_name="dataset-a",
            status="running",
            created_at="2026-03-14T00:00:00+00:00",
            total_frames=3,
            fps=2.0,
        )
    )

    frame_repo.add(
        FrameEvent(
            mission_id="mission-1",
            frame_id=2,
            ts_sec=1.0,
            image_uri="file:///frame-2.jpg",
            gt_person_present=False,
            gt_episode_id=None,
        )
    )
    frame_repo.add(
        FrameEvent(
            mission_id="mission-1",
            frame_id=1,
            ts_sec=0.5,
            image_uri="file:///frame-1.jpg",
            gt_person_present=True,
            gt_episode_id="ep-1",
        )
    )

    frames = frame_repo.list_by_mission("mission-1")

    assert [frame.frame_id for frame in frames] == [1, 2]
    assert frames[0].gt_person_present is True
    assert frames[1].gt_person_present is False


@pytest.mark.integration
def test_postgres_pilot_service_report_flow_persists_episode_projection(
    postgres_runtime: dict[str, Any],
) -> None:
    db = postgres_runtime["db"]
    service = PilotService(
        dependencies=PilotService.Dependencies(
            mission_repository=postgres_runtime["mission_repo"],
            alert_repository=postgres_runtime["alert_repo"],
            frame_event_repository=postgres_runtime["frame_repo"],
            artifact_storage=InMemoryArtifactStorage(),
        )
    )
    mission = service.create_mission(source_name="dataset-a", total_frames=5, fps=2.0)
    started = service.start_mission(mission.mission_id)
    assert started is not None

    first_alert_id = service.ingest_frame_event(
        frame_event=FrameEvent(
            mission_id=mission.mission_id,
            frame_id=0,
            ts_sec=0.0,
            image_uri="file:///frame-0.jpg",
            gt_person_present=True,
            gt_episode_id="ep-1",
        ),
        detections=[
            DetectionInput(
                bbox=(10.0, 20.0, 30.0, 40.0),
                score=0.95,
                label="person",
                model_name="yolo8n",
                explanation="alert-1",
            )
        ],
    )[0].alert_id
    service.ingest_frame_event(
        frame_event=FrameEvent(
            mission_id=mission.mission_id,
            frame_id=1,
            ts_sec=0.5,
            image_uri="file:///frame-1.jpg",
            gt_person_present=True,
            gt_episode_id="ep-1",
        ),
        detections=[],
    )
    second_alert_id = service.ingest_frame_event(
        frame_event=FrameEvent(
            mission_id=mission.mission_id,
            frame_id=3,
            ts_sec=3.2,
            image_uri="file:///frame-3.jpg",
            gt_person_present=True,
            gt_episode_id="ep-2",
        ),
        detections=[
            DetectionInput(
                bbox=(50.0, 60.0, 70.0, 80.0),
                score=0.96,
                label="person",
                model_name="yolo8n",
                explanation="alert-2",
            )
        ],
    )[0].alert_id
    service.ingest_frame_event(
        frame_event=FrameEvent(
            mission_id=mission.mission_id,
            frame_id=4,
            ts_sec=3.7,
            image_uri="file:///frame-4.jpg",
            gt_person_present=True,
            gt_episode_id="ep-2",
        ),
        detections=[],
    )

    confirmed = service.review_alert(
        first_alert_id,
        {
            "status": "reviewed_confirmed",
            "reviewed_by": "operator-1",
            "reviewed_at_sec": 0.9,
            "decision_reason": "valid target",
        },
    )
    rejected = service.review_alert(
        second_alert_id,
        {
            "status": "reviewed_rejected",
            "reviewed_by": "operator-1",
            "reviewed_at_sec": 3.9,
            "decision_reason": "false positive",
        },
    )
    assert confirmed is not None
    assert rejected is not None

    report = service.get_mission_report(mission.mission_id)

    assert report["episodes_total"] == 2
    assert report["episodes_found"] == 2
    assert report["alerts_total"] == 2
    assert report["alerts_confirmed"] == 1
    assert report["alerts_rejected"] == 1
    assert report["false_alerts_total"] == 0

    episodes = _fetch_episodes(db=db, mission_id=mission.mission_id)
    assert len(episodes) == 2
    assert episodes[0]["episode_index"] == 1
    assert episodes[0]["found_by_confirmed_alert"] is True
    assert episodes[1]["episode_index"] == 2
    assert episodes[1]["found_by_confirmed_alert"] is False


def _ensure_test_schema(db: PostgresDatabase) -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS missions (
            mission_id TEXT PRIMARY KEY,
            source_name TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            total_frames INTEGER NOT NULL,
            fps DOUBLE PRECISION NOT NULL,
            completed_frame_id INTEGER NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_missions_status ON missions (status)",
        """
        CREATE TABLE IF NOT EXISTS frame_events (
            mission_id TEXT NOT NULL REFERENCES missions (mission_id) ON DELETE CASCADE,
            frame_id INTEGER NOT NULL,
            ts_sec DOUBLE PRECISION NOT NULL,
            image_uri TEXT NOT NULL,
            gt_person_present BOOLEAN NOT NULL,
            gt_episode_id TEXT NULL,
            PRIMARY KEY (mission_id, frame_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_frame_events_mission_ts
        ON frame_events (mission_id, ts_sec)
        """,
        """
        CREATE TABLE IF NOT EXISTS alerts (
            alert_id TEXT PRIMARY KEY,
            mission_id TEXT NOT NULL REFERENCES missions (mission_id) ON DELETE CASCADE,
            frame_id INTEGER NOT NULL,
            ts_sec DOUBLE PRECISION NOT NULL,
            image_uri TEXT NOT NULL,
            people_detected INTEGER NOT NULL,
            primary_bbox JSONB NOT NULL,
            primary_score DOUBLE PRECISION NOT NULL,
            primary_label TEXT NOT NULL,
            primary_model_name TEXT NOT NULL,
            primary_explanation TEXT NULL,
            detections JSONB NOT NULL,
            status TEXT NOT NULL,
            reviewed_by TEXT NULL,
            reviewed_at_sec DOUBLE PRECISION NULL,
            decision_reason TEXT NULL,
            CONSTRAINT fk_alert_frame
                FOREIGN KEY (mission_id, frame_id)
                REFERENCES frame_events (mission_id, frame_id)
                ON DELETE CASCADE
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_alerts_mission_ts
        ON alerts (mission_id, ts_sec)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_alerts_mission_status
        ON alerts (mission_id, status)
        """,
        """
        CREATE TABLE IF NOT EXISTS episodes (
            mission_id TEXT NOT NULL REFERENCES missions (mission_id) ON DELETE CASCADE,
            episode_index INTEGER NOT NULL,
            start_sec DOUBLE PRECISION NOT NULL,
            end_sec DOUBLE PRECISION NOT NULL,
            found_by_confirmed_alert BOOLEAN NOT NULL DEFAULT FALSE,
            PRIMARY KEY (mission_id, episode_index)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_episodes_mission_found
        ON episodes (mission_id, found_by_confirmed_alert)
        """,
    ]
    with db.connect() as conn:
        with conn.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
        conn.commit()


def _fetch_episodes(
    *,
    db: PostgresDatabase,
    mission_id: str,
) -> list[dict[str, object]]:
    with db.connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT episode_index, start_sec, end_sec, found_by_confirmed_alert
                FROM episodes
                WHERE mission_id = %s
                ORDER BY episode_index
                """,
                (mission_id,),
            )
            rows = cursor.fetchall()
    return [
        {
            "episode_index": int(row[0]),
            "start_sec": float(row[1]),
            "end_sec": float(row[2]),
            "found_by_confirmed_alert": bool(row[3]),
        }
        for row in rows
    ]
