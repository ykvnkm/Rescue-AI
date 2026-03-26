"""Integration tests for Postgres repositories with a real database."""

from __future__ import annotations

import pytest

from rescue_ai.domain.entities import Alert, Detection, FrameEvent, Mission
from rescue_ai.infrastructure.postgres_repositories import (
    EpisodeProjectionSettings,
    PostgresAlertRepository,
    PostgresDatabase,
    PostgresFrameEventRepository,
    PostgresMissionRepository,
)


def _mission(mid: str = "m-1", **kw: object) -> Mission:
    defaults = dict(
        mission_id=mid,
        source_name="dataset-a",
        status="created",
        created_at="2026-03-14T00:00:00+00:00",
        total_frames=10,
        fps=2.5,
    )
    defaults.update(kw)
    return Mission(**defaults)  # type: ignore[arg-type]


def _frame(mid: str = "m-1", fid: int = 1, **kw: object) -> FrameEvent:
    defaults = dict(
        mission_id=mid,
        frame_id=fid,
        ts_sec=0.5 * fid,
        image_uri=f"s3://bucket/frames/{fid}.jpg",
        gt_person_present=False,
        gt_episode_id=None,
    )
    defaults.update(kw)
    return FrameEvent(**defaults)  # type: ignore[arg-type]


def _detection(**kw: object) -> Detection:
    defaults = dict(
        bbox=(10.0, 20.0, 30.0, 40.0),
        score=0.95,
        label="person",
        model_name="yolo8n",
        explanation="hit",
    )
    defaults.update(kw)
    return Detection(**defaults)  # type: ignore[arg-type]


def _alert(aid: str = "a-1", mid: str = "m-1", fid: int = 1, **kw: object) -> Alert:
    det = _detection()
    defaults = dict(
        alert_id=aid,
        mission_id=mid,
        frame_id=fid,
        ts_sec=0.5,
        image_uri="s3://bucket/frames/1.jpg",
        people_detected=1,
        primary_detection=det,
        detections=[det],
        status="queued",
    )
    defaults.update(kw)
    return Alert(**defaults)  # type: ignore[arg-type]


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
        status="reviewed_confirmed",
        reviewed_by="operator-1",
        reviewed_at_sec=1.0,
        decision_reason="valid",
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
    alerts.update_status("a-1", status="reviewed_confirmed")

    with pytest.raises(ValueError, match="already reviewed"):
        alerts.update_status("a-1", status="reviewed_rejected")


@pytest.mark.integration
def test_update_invalid_status_raises(pg_db: PostgresDatabase) -> None:
    alerts = PostgresAlertRepository(pg_db)
    with pytest.raises(ValueError, match="Invalid target status"):
        alerts.update_status("a-1", status="bogus")


@pytest.mark.integration
def test_update_returns_none_for_missing(pg_db: PostgresDatabase) -> None:
    alerts = PostgresAlertRepository(pg_db)
    assert alerts.update_status("nope", status="reviewed_confirmed") is None


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
