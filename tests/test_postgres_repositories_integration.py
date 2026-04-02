"""Integration tests for Postgres repositories with a real database."""

from __future__ import annotations

from typing import cast

import pytest

from rescue_ai.domain.entities import Alert, Detection, FrameEvent, Mission
from rescue_ai.domain.ports import AlertReviewPayload
from rescue_ai.domain.value_objects import AlertStatus
from rescue_ai.infrastructure.postgres_repositories import (
    PostgresAlertRepository,
    PostgresDatabase,
    PostgresFrameEventRepository,
    PostgresMissionRepository,
)


def _mission(
    mid: str = "m-1",
    status: str = "created",
) -> Mission:
    return Mission(
        mission_id=mid,
        source_name="dataset-a",
        status=status,
        created_at="2026-03-14T00:00:00+00:00",
        total_frames=10,
        fps=2.5,
    )


def _frame(
    mid: str = "m-1",
    fid: int = 1,
    ts_sec: float | None = None,
) -> FrameEvent:
    return FrameEvent(
        mission_id=mid,
        frame_id=fid,
        ts_sec=ts_sec if ts_sec is not None else 0.5 * fid,
        image_uri=f"s3://bucket/frames/{fid}.jpg",
        gt_person_present=False,
        gt_episode_id=None,
    )


def _detection() -> Detection:
    return Detection(
        bbox=(10.0, 20.0, 30.0, 40.0),
        score=0.95,
        label="person",
        model_name="yolo8n",
        explanation="hit",
    )


def _alert(aid: str = "a-1", mid: str = "m-1", fid: int = 1) -> Alert:
    det = _detection()
    return Alert(
        alert_id=aid,
        mission_id=mid,
        frame_id=fid,
        ts_sec=0.5,
        image_uri="s3://bucket/frames/1.jpg",
        people_detected=1,
        primary_detection=det,
        detections=[det],
        status=AlertStatus.QUEUED,
    )


# ── Mission Repository ──────────────────────────────────────────────────


@pytest.mark.integration
def test_create_and_get_mission(pg_db: PostgresDatabase) -> None:
    repo = PostgresMissionRepository(pg_db)
    repo.create(_mission())

    stored = repo.get("m-1")
    assert stored is not None
    assert stored.source_name == "dataset-a"
    assert stored.total_frames == 10
    assert stored.fps == 2.5


@pytest.mark.integration
def test_get_returns_none_for_unknown_id(pg_db: PostgresDatabase) -> None:
    repo = PostgresMissionRepository(pg_db)
    assert repo.get("does-not-exist") is None


@pytest.mark.integration
def test_list_missions_with_status_filter(pg_db: PostgresDatabase) -> None:
    repo = PostgresMissionRepository(pg_db)
    repo.create(_mission("m-1", status="created"))
    repo.create(_mission("m-2", status="running"))
    repo.create(_mission("m-3", status="completed"))

    running = repo.list(status="running")
    assert [item.mission_id for item in running] == ["m-2"]


@pytest.mark.integration
def test_update_details(pg_db: PostgresDatabase) -> None:
    repo = PostgresMissionRepository(pg_db)
    repo.create(_mission())

    updated = repo.update_details("m-1", total_frames=42, fps=6.0)
    assert updated is not None
    assert updated.total_frames == 42
    assert updated.fps == 6.0
    assert updated.source_name == "dataset-a"


@pytest.mark.integration
def test_update_status(pg_db: PostgresDatabase) -> None:
    repo = PostgresMissionRepository(pg_db)
    repo.create(_mission())

    completed = repo.update_status("m-1", "completed", completed_frame_id=9)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.completed_frame_id == 9


@pytest.mark.integration
def test_update_status_returns_none_for_missing(pg_db: PostgresDatabase) -> None:
    repo = PostgresMissionRepository(pg_db)
    assert repo.update_status("nope", "completed") is None


# ── FrameEvent Repository ───────────────────────────────────────────────


@pytest.mark.integration
def test_add_and_list_by_mission(pg_db: PostgresDatabase) -> None:
    missions = PostgresMissionRepository(pg_db)
    frames = PostgresFrameEventRepository(pg_db)
    missions.create(_mission())

    frames.add(_frame(fid=2, ts_sec=1.0))
    frames.add(_frame(fid=1, ts_sec=0.5))

    listed = frames.list_by_mission("m-1")
    assert [f.frame_id for f in listed] == [1, 2]


@pytest.mark.integration
def test_upsert_overwrites_on_conflict(pg_db: PostgresDatabase) -> None:
    missions = PostgresMissionRepository(pg_db)
    frames = PostgresFrameEventRepository(pg_db)
    missions.create(_mission())

    frames.add(_frame(fid=1, ts_sec=0.5))
    frames.add(_frame(fid=1, ts_sec=1.5))

    listed = frames.list_by_mission("m-1")
    assert len(listed) == 1
    assert listed[0].ts_sec == 1.5


@pytest.mark.integration
def test_list_by_mission_returns_empty_for_unknown(pg_db: PostgresDatabase) -> None:
    frames = PostgresFrameEventRepository(pg_db)
    assert frames.list_by_mission("nope") == []


# ── Alert Repository ────────────────────────────────────────────────────


@pytest.mark.integration
def test_add_and_get_alert(pg_db: PostgresDatabase) -> None:
    missions = PostgresMissionRepository(pg_db)
    frames = PostgresFrameEventRepository(pg_db)
    alerts = PostgresAlertRepository(pg_db)

    missions.create(_mission())
    frames.add(_frame(fid=1))
    alerts.add(_alert())

    stored = alerts.get("a-1")
    assert stored is not None
    assert stored.people_detected == 1
    assert stored.primary_detection.bbox == (10.0, 20.0, 30.0, 40.0)
    assert stored.primary_detection.score == 0.95
    assert len(stored.detections) == 1


@pytest.mark.integration
def test_list_filters_by_mission_and_status(pg_db: PostgresDatabase) -> None:
    missions = PostgresMissionRepository(pg_db)
    frames = PostgresFrameEventRepository(pg_db)
    alerts = PostgresAlertRepository(pg_db)

    missions.create(_mission("m-1"))
    missions.create(_mission("m-2"))
    frames.add(_frame("m-1", 1))
    frames.add(_frame("m-2", 1))
    alerts.add(_alert("a-1", "m-1"))
    alerts.add(_alert("a-2", "m-2"))

    listed = alerts.list(mission_id="m-1", status="queued")
    assert [a.alert_id for a in listed] == ["a-1"]


@pytest.mark.integration
def test_update_alert_status(pg_db: PostgresDatabase) -> None:
    missions = PostgresMissionRepository(pg_db)
    frames = PostgresFrameEventRepository(pg_db)
    alerts = PostgresAlertRepository(pg_db)

    missions.create(_mission())
    frames.add(_frame())
    alerts.add(_alert())

    reviewed = alerts.update_status(
        "a-1",
        {
            "status": AlertStatus.REVIEWED_CONFIRMED,
            "reviewed_by": "operator-1",
            "reviewed_at_sec": 1.0,
            "decision_reason": "valid",
        },
    )
    assert reviewed is not None
    assert reviewed.status == "reviewed_confirmed"
    assert reviewed.reviewed_by == "operator-1"


@pytest.mark.integration
def test_update_already_reviewed_raises(pg_db: PostgresDatabase) -> None:
    missions = PostgresMissionRepository(pg_db)
    frames = PostgresFrameEventRepository(pg_db)
    alerts = PostgresAlertRepository(pg_db)

    missions.create(_mission())
    frames.add(_frame())
    alerts.add(_alert())
    alerts.update_status(
        "a-1",
        {
            "status": AlertStatus.REVIEWED_CONFIRMED,
            "reviewed_by": None,
            "reviewed_at_sec": None,
            "decision_reason": None,
        },
    )

    with pytest.raises(ValueError, match="already reviewed"):
        alerts.update_status(
            "a-1",
            {
                "status": AlertStatus.REVIEWED_REJECTED,
                "reviewed_by": None,
                "reviewed_at_sec": None,
                "decision_reason": None,
            },
        )


@pytest.mark.integration
def test_update_invalid_status_raises(pg_db: PostgresDatabase) -> None:
    alerts = PostgresAlertRepository(pg_db)
    bogus_payload = cast(
        AlertReviewPayload,
        {
            "status": "bogus",
            "reviewed_by": None,
            "reviewed_at_sec": None,
            "decision_reason": None,
        },
    )
    with pytest.raises(ValueError, match="Invalid target status"):
        alerts.update_status("a-1", bogus_payload)


@pytest.mark.integration
def test_update_returns_none_for_missing(pg_db: PostgresDatabase) -> None:
    alerts = PostgresAlertRepository(pg_db)
    assert (
        alerts.update_status(
            "nope",
            {
                "status": AlertStatus.REVIEWED_CONFIRMED,
                "reviewed_by": None,
                "reviewed_at_sec": None,
                "decision_reason": None,
            },
        )
        is None
    )


# ── PostgresDatabase ────────────────────────────────────────────────────


@pytest.mark.integration
def test_truncate_all(pg_db: PostgresDatabase) -> None:
    missions = PostgresMissionRepository(pg_db)
    frames = PostgresFrameEventRepository(pg_db)
    alerts = PostgresAlertRepository(pg_db)

    missions.create(_mission())
    frames.add(_frame())
    alerts.add(_alert())

    pg_db.truncate_all()

    assert missions.get("m-1") is None
    assert alerts.get("a-1") is None
    assert frames.list_by_mission("m-1") == []
