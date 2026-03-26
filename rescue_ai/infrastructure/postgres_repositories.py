"""Postgres implementations of repository ports."""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from rescue_ai.application.mission_metrics import build_gt_episodes
from rescue_ai.domain.entities import Alert, Detection, FrameEvent, Mission

MISSION_COLUMNS = """
mission_id,
source_name,
status,
created_at,
total_frames,
fps,
completed_frame_id
"""

ALERT_COLUMNS = """
alert_id,
mission_id,
frame_id,
ts_sec,
image_uri,
people_detected,
primary_bbox,
primary_score,
primary_label,
primary_model_name,
primary_explanation,
detections,
status,
reviewed_by,
reviewed_at_sec,
decision_reason
"""

FRAME_EVENT_COLUMNS = """
mission_id,
frame_id,
ts_sec,
image_uri,
gt_person_present,
gt_episode_id
"""


@dataclass(frozen=True)
class EpisodeProjectionSettings:
    """Settings used to keep the `episodes` projection aligned with reports."""

    gt_gap_end_sec: float
    match_tolerance_sec: float


class PostgresDatabase:
    """Thin wrapper around a psycopg DSN used by repository adapters."""

    def __init__(self, dsn: str) -> None:
        try:
            psycopg = importlib.import_module("psycopg")
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "psycopg is required for APP_REPOSITORY_BACKEND=postgres"
            ) from exc

        self._psycopg = psycopg
        self._dsn = dsn

    def connect(self) -> Any:
        """Open a new connection to the database."""
        return self._psycopg.connect(self._dsn)

    def truncate_all(self) -> None:
        with self.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    TRUNCATE TABLE episodes, alerts, frame_events, missions CASCADE
                    """
                )
            conn.commit()


class _EpisodeProjectionStore:
    """Refreshes `episodes` rows from stored frame events and alert matches."""

    def __init__(self, settings: EpisodeProjectionSettings) -> None:
        self._settings = settings

    def settings(self) -> EpisodeProjectionSettings:
        return self._settings

    def refresh(self, conn: Any, mission_id: str) -> None:
        frames = self._load_frames(conn=conn, mission_id=mission_id)
        alert_ts = self._load_alert_timestamps(
            conn=conn,
            mission_id=mission_id,
        )
        episodes = build_gt_episodes(
            frames=frames,
            gt_gap_end_sec=self._settings.gt_gap_end_sec,
        )

        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM episodes WHERE mission_id = %s",
                (mission_id,),
            )
            if not episodes:
                return

            rows = [
                (
                    mission_id,
                    episode_index,
                    start_sec,
                    end_sec,
                    _episode_found_by_alert(
                        start_sec=start_sec,
                        end_sec=end_sec,
                        alert_ts=alert_ts,
                        tolerance_sec=self._settings.match_tolerance_sec,
                    ),
                )
                for episode_index, (start_sec, end_sec) in enumerate(episodes, start=1)
            ]
            cursor.executemany(
                """
                INSERT INTO episodes (
                    mission_id,
                    episode_index,
                    start_sec,
                    end_sec,
                    found_by_alert
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                rows,
            )

    def _load_frames(self, conn: Any, mission_id: str) -> list[FrameEvent]:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT {FRAME_EVENT_COLUMNS}
                FROM frame_events
                WHERE mission_id = %s
                ORDER BY frame_id
                """,
                (mission_id,),
            )
            return [_frame_event_from_row(row) for row in cursor.fetchall()]

    def _load_alert_timestamps(self, conn: Any, mission_id: str) -> list[float]:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT ts_sec
                FROM alerts
                WHERE mission_id = %s
                ORDER BY ts_sec
                """,
                (mission_id,),
            )
            return [float(row[0]) for row in cursor.fetchall()]


class PostgresMissionRepository:
    """Postgres implementation of mission repository."""

    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    def create(self, mission: Mission) -> None:
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO missions (
                        mission_id,
                        source_name,
                        status,
                        created_at,
                        total_frames,
                        fps,
                        completed_frame_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        mission.mission_id,
                        mission.source_name,
                        mission.status,
                        _parse_iso_datetime(mission.created_at),
                        mission.total_frames,
                        mission.fps,
                        mission.completed_frame_id,
                    ),
                )
            conn.commit()

    def get(self, mission_id: str) -> Mission | None:
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT {MISSION_COLUMNS}
                    FROM missions
                    WHERE mission_id = %s
                    """,
                    (mission_id,),
                )
                row = cursor.fetchone()
        return None if row is None else _mission_from_row(row)

    def update_details(
        self,
        mission_id: str,
        *,
        source_name: str | None = None,
        total_frames: int | None = None,
        fps: float | None = None,
    ) -> Mission | None:
        if source_name is None and total_frames is None and fps is None:
            return self.get(mission_id)

        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE missions
                    SET
                        source_name = COALESCE(%s, source_name),
                        total_frames = COALESCE(%s, total_frames),
                        fps = COALESCE(%s, fps)
                    WHERE mission_id = %s
                    RETURNING {MISSION_COLUMNS}
                    """,
                    (source_name, total_frames, fps, mission_id),
                )
                row = cursor.fetchone()
            conn.commit()
        return None if row is None else _mission_from_row(row)

    def update_status(
        self,
        mission_id: str,
        status: str,
        completed_frame_id: int | None = None,
    ) -> Mission | None:
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE missions
                    SET
                        status = %s,
                        completed_frame_id = COALESCE(%s, completed_frame_id)
                    WHERE mission_id = %s
                    RETURNING {MISSION_COLUMNS}
                    """,
                    (status, completed_frame_id, mission_id),
                )
                row = cursor.fetchone()
            conn.commit()
        return None if row is None else _mission_from_row(row)


class PostgresAlertRepository:
    """Postgres implementation of alert repository."""

    allowed_target_statuses = {"reviewed_confirmed", "reviewed_rejected"}

    def __init__(
        self,
        db: PostgresDatabase,
        episode_settings: EpisodeProjectionSettings | None = None,
    ) -> None:
        self._db = db
        self._episodes = (
            _EpisodeProjectionStore(episode_settings)
            if episode_settings is not None
            else None
        )

    def add(self, alert: Alert) -> None:
        primary = alert.primary_detection
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO alerts (
                        alert_id,
                        mission_id,
                        frame_id,
                        ts_sec,
                        image_uri,
                        people_detected,
                        primary_bbox,
                        primary_score,
                        primary_label,
                        primary_model_name,
                        primary_explanation,
                        detections,
                        status,
                        reviewed_by,
                        reviewed_at_sec,
                        decision_reason
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s::jsonb, %s, %s, %s, %s,
                        %s::jsonb, %s, %s, %s, %s
                    )
                    """,
                    (
                        alert.alert_id,
                        alert.mission_id,
                        alert.frame_id,
                        alert.ts_sec,
                        alert.image_uri,
                        alert.people_detected,
                        json.dumps(list(primary.bbox)),
                        primary.score,
                        primary.label,
                        primary.model_name,
                        primary.explanation,
                        json.dumps(
                            [
                                _serialize_detection(detection)
                                for detection in alert.detections
                            ]
                        ),
                        alert.status,
                        alert.reviewed_by,
                        alert.reviewed_at_sec,
                        alert.decision_reason,
                    ),
                )
                if self._episodes is not None:
                    self._episodes.refresh(conn=conn, mission_id=alert.mission_id)
            conn.commit()

    def get(self, alert_id: str) -> Alert | None:
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT {ALERT_COLUMNS}
                    FROM alerts
                    WHERE alert_id = %s
                    """,
                    (alert_id,),
                )
                row = cursor.fetchone()
        return None if row is None else _alert_from_row(row)

    def list(
        self,
        mission_id: str | None = None,
        status: str | None = None,
    ) -> list[Alert]:
        clauses: list[str] = []
        params: list[object] = []
        if mission_id is not None:
            clauses.append("mission_id = %s")
            params.append(mission_id)
        if status is not None:
            clauses.append("status = %s")
            params.append(status)

        where_clause = ""
        if clauses:
            where_clause = "WHERE " + " AND ".join(clauses)

        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT {ALERT_COLUMNS}
                    FROM alerts
                    {where_clause}
                    ORDER BY ts_sec, frame_id
                    """,
                    tuple(params),
                )
                return [_alert_from_row(row) for row in cursor.fetchall()]

    def update_status(
        self,
        alert_id: str,
        *,
        status: str,
        reviewed_by: str | None = None,
        reviewed_at_sec: float | None = None,
        decision_reason: str | None = None,
    ) -> Alert | None:
        if status not in self.allowed_target_statuses:
            raise ValueError("Invalid target status")

        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT mission_id, status, ts_sec
                    FROM alerts
                    WHERE alert_id = %s
                    FOR UPDATE
                    """,
                    (alert_id,),
                )
                existing = cursor.fetchone()
                if existing is None:
                    return None
                if str(existing[1]) != "queued":
                    raise ValueError("Alert already reviewed")

                effective_reviewed_at = (
                    reviewed_at_sec
                    if reviewed_at_sec is not None
                    else float(existing[2])
                )
                cursor.execute(
                    f"""
                    UPDATE alerts
                    SET
                        status = %s,
                        reviewed_by = %s,
                        reviewed_at_sec = %s,
                        decision_reason = %s
                    WHERE alert_id = %s
                    RETURNING {ALERT_COLUMNS}
                    """,
                    (
                        status,
                        reviewed_by,
                        effective_reviewed_at,
                        decision_reason,
                        alert_id,
                    ),
                )
                row = cursor.fetchone()
                if row is not None and self._episodes is not None:
                    self._episodes.refresh(conn=conn, mission_id=str(existing[0]))
            conn.commit()
        return None if row is None else _alert_from_row(row)


class PostgresFrameEventRepository:
    """Postgres implementation of frame event repository."""

    def __init__(
        self,
        db: PostgresDatabase,
        episode_settings: EpisodeProjectionSettings | None = None,
    ) -> None:
        self._db = db
        self._episodes = (
            _EpisodeProjectionStore(episode_settings)
            if episode_settings is not None
            else None
        )

    def add(self, frame_event: FrameEvent) -> None:
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO frame_events (
                        mission_id,
                        frame_id,
                        ts_sec,
                        image_uri,
                        gt_person_present,
                        gt_episode_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (mission_id, frame_id)
                    DO UPDATE SET
                        ts_sec = EXCLUDED.ts_sec,
                        image_uri = EXCLUDED.image_uri,
                        gt_person_present = EXCLUDED.gt_person_present,
                        gt_episode_id = EXCLUDED.gt_episode_id
                    """,
                    (
                        frame_event.mission_id,
                        frame_event.frame_id,
                        frame_event.ts_sec,
                        frame_event.image_uri,
                        frame_event.gt_person_present,
                        frame_event.gt_episode_id,
                    ),
                )
                if self._episodes is not None:
                    self._episodes.refresh(conn=conn, mission_id=frame_event.mission_id)
            conn.commit()

    def list_by_mission(self, mission_id: str) -> list[FrameEvent]:
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT {FRAME_EVENT_COLUMNS}
                    FROM frame_events
                    WHERE mission_id = %s
                    ORDER BY frame_id
                    """,
                    (mission_id,),
                )
                return [_frame_event_from_row(row) for row in cursor.fetchall()]


# ── Row mappers ──────────────────────────────────────────────────


def _mission_from_row(row: Sequence[Any]) -> Mission:
    return Mission(
        mission_id=str(row[0]),
        source_name=str(row[1]),
        status=str(row[2]),
        created_at=_as_iso_datetime(row[3]),
        total_frames=int(row[4]),
        fps=float(row[5]),
        completed_frame_id=None if row[6] is None else int(row[6]),
    )


def _alert_from_row(row: Sequence[Any]) -> Alert:
    primary_bbox = _coerce_bbox(row[6])
    detections_payload = _load_json_value(row[11])
    detections: list[Detection] = []
    if isinstance(detections_payload, list):
        detections = [
            _detection_from_payload(item)
            for item in detections_payload
            if isinstance(item, dict)
        ]

    return Alert(
        alert_id=str(row[0]),
        mission_id=str(row[1]),
        frame_id=int(row[2]),
        ts_sec=float(row[3]),
        image_uri=str(row[4]),
        people_detected=int(row[5]),
        primary_detection=Detection(
            bbox=primary_bbox,
            score=float(row[7]),
            label=str(row[8]),
            model_name=str(row[9]),
            explanation=None if row[10] is None else str(row[10]),
        ),
        detections=detections,
        status=str(row[12]),
        reviewed_by=None if row[13] is None else str(row[13]),
        reviewed_at_sec=None if row[14] is None else float(row[14]),
        decision_reason=None if row[15] is None else str(row[15]),
    )


def _frame_event_from_row(row: Sequence[Any]) -> FrameEvent:
    return FrameEvent(
        mission_id=str(row[0]),
        frame_id=int(row[1]),
        ts_sec=float(row[2]),
        image_uri=str(row[3]),
        gt_person_present=bool(row[4]),
        gt_episode_id=None if row[5] is None else str(row[5]),
    )


def _serialize_detection(detection: Detection) -> dict[str, object]:
    return {
        "bbox": list(detection.bbox),
        "score": detection.score,
        "label": detection.label,
        "model_name": detection.model_name,
        "explanation": detection.explanation,
    }


def _detection_from_payload(payload: dict[str, Any]) -> Detection:
    return Detection(
        bbox=_coerce_bbox(payload.get("bbox")),
        score=float(payload.get("score", 0.0)),
        label=str(payload.get("label", "person")),
        model_name=str(payload.get("model_name", "unknown")),
        explanation=(
            None
            if payload.get("explanation") is None
            else str(payload.get("explanation"))
        ),
    )


def _coerce_bbox(value: Any) -> tuple[float, float, float, float]:
    payload = _load_json_value(value)
    if not isinstance(payload, list) or len(payload) != 4:
        raise ValueError("Invalid bbox payload")
    return (float(payload[0]), float(payload[1]), float(payload[2]), float(payload[3]))


def _load_json_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return value


def _parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _as_iso_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        normalized = (
            value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        )
        return normalized.isoformat()
    return str(value)


def _episode_found_by_alert(
    *,
    start_sec: float,
    end_sec: float,
    alert_ts: list[float],
    tolerance_sec: float,
) -> bool:
    window_start = start_sec - tolerance_sec
    window_end = end_sec + tolerance_sec
    return any(window_start <= ts_sec <= window_end for ts_sec in alert_ts)
