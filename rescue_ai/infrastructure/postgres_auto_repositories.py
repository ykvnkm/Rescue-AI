"""Postgres implementations of automatic-mode repository ports.

These are the persistence adapters wired behind
:class:`TrajectoryRepository` and :class:`AutoDecisionRepository`
(defined in :mod:`rescue_ai.domain.ports`). They correspond to the SQL
tables created by ``infra/postgres/init/011-auto-mode-schema.sql``.

``AutoMissionConfigRepository`` is an infrastructure-only helper (no
matching domain port yet) used by :class:`AutoMissionService` to persist
the per-mission ``auto_mission_config`` snapshot.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Sequence

from rescue_ai.domain.entities import AutoDecision, TrajectoryPoint
from rescue_ai.domain.value_objects import AutoDecisionKind, NavMode, TrajectorySource
from rescue_ai.infrastructure.postgres_connection import PostgresDatabase

TRAJECTORY_COLUMNS = """
mission_id,
seq,
ts_sec,
frame_id,
x,
y,
z,
source
"""

DECISION_COLUMNS = """
decision_id,
mission_id,
frame_id,
ts_sec,
kind,
reason,
created_at
"""


class PostgresTrajectoryRepository:
    """Postgres-backed ``TrajectoryRepository`` writing to
    ``auto_trajectory_points``.
    """

    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    def add(self, point: TrajectoryPoint) -> None:
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO auto_trajectory_points (
                        mission_id, seq, ts_sec, frame_id, x, y, z, source
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (mission_id, seq) DO UPDATE SET
                        ts_sec = EXCLUDED.ts_sec,
                        frame_id = EXCLUDED.frame_id,
                        x = EXCLUDED.x,
                        y = EXCLUDED.y,
                        z = EXCLUDED.z,
                        source = EXCLUDED.source
                    """,
                    (
                        point.mission_id,
                        point.seq,
                        point.ts_sec,
                        point.frame_id,
                        point.x,
                        point.y,
                        point.z,
                        str(point.source),
                    ),
                )
            conn.commit()

    def list_by_mission(self, mission_id: str) -> list[TrajectoryPoint]:
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT {TRAJECTORY_COLUMNS}
                    FROM auto_trajectory_points
                    WHERE mission_id = %s
                    ORDER BY seq
                    """,
                    (mission_id,),
                )
                return [_trajectory_from_row(row) for row in cursor.fetchall()]


class PostgresAutoDecisionRepository:
    """Postgres-backed ``AutoDecisionRepository`` writing to ``auto_decisions``."""

    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    def add(self, decision: AutoDecision) -> None:
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO auto_decisions (
                        decision_id, mission_id, frame_id, ts_sec, kind,
                        reason, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (decision_id) DO NOTHING
                    """,
                    (
                        decision.decision_id,
                        decision.mission_id,
                        decision.frame_id,
                        decision.ts_sec,
                        str(decision.kind),
                        decision.reason,
                        _parse_iso_datetime(decision.created_at),
                    ),
                )
            conn.commit()

    def list_by_mission(self, mission_id: str) -> list[AutoDecision]:
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT {DECISION_COLUMNS}
                    FROM auto_decisions
                    WHERE mission_id = %s
                    ORDER BY ts_sec, decision_id
                    """,
                    (mission_id,),
                )
                return [_decision_from_row(row) for row in cursor.fetchall()]


class PostgresAutoMissionConfigRepository:
    """Persists the one-shot ``auto_mission_config`` snapshot per mission."""

    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    def save(
        self,
        *,
        mission_id: str,
        nav_mode: NavMode,
        detector: str,
        config_json: Mapping[str, object],
    ) -> None:
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO auto_mission_config (
                        mission_id, nav_mode, detector, config_json
                    )
                    VALUES (%s, %s, %s, %s::jsonb)
                    ON CONFLICT (mission_id) DO UPDATE SET
                        nav_mode = EXCLUDED.nav_mode,
                        detector = EXCLUDED.detector,
                        config_json = EXCLUDED.config_json
                    """,
                    (
                        mission_id,
                        str(nav_mode),
                        detector,
                        json.dumps(config_json),
                    ),
                )
            conn.commit()

    def get(self, mission_id: str) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT nav_mode, detector, config_json
                    FROM auto_mission_config
                    WHERE mission_id = %s
                    """,
                    (mission_id,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return {
            "nav_mode": str(row[0]),
            "detector": str(row[1]),
            "config_json": _load_json_value(row[2]),
        }


def _trajectory_from_row(row: Sequence[Any]) -> TrajectoryPoint:
    return TrajectoryPoint(
        mission_id=str(row[0]),
        seq=int(row[1]),
        ts_sec=float(row[2]),
        frame_id=None if row[3] is None else int(row[3]),
        x=float(row[4]),
        y=float(row[5]),
        z=float(row[6]),
        source=TrajectorySource(str(row[7])),
    )


def _decision_from_row(row: Sequence[Any]) -> AutoDecision:
    return AutoDecision(
        decision_id=str(row[0]),
        mission_id=str(row[1]),
        frame_id=None if row[2] is None else int(row[2]),
        ts_sec=float(row[3]),
        kind=AutoDecisionKind(str(row[4])),
        reason=str(row[5]),
        created_at=_as_iso_datetime(row[6]),
    )


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


def _load_json_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return value
