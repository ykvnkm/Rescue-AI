"""JSON and Postgres status store implementations."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

from rescue_ai.application.batch_runner import RunStatusRecord


class JsonStatusStore:
    """Status store backed by a local JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def get(self, run_key: str) -> RunStatusRecord | None:
        data = self._read_all()
        payload = data.get(run_key)
        if payload is None:
            return None
        return RunStatusRecord(
            run_key=run_key,
            status=str(payload.get("status", "pending")),
            reason=_as_optional_str(payload.get("reason")),
            report_uri=_as_optional_str(payload.get("report_uri")),
            debug_uri=_as_optional_str(payload.get("debug_uri")),
        )

    def upsert(self, record: RunStatusRecord) -> None:
        data = self._read_all()
        data[record.run_key] = {
            "status": record.status,
            "reason": record.reason,
            "report_uri": record.report_uri,
            "debug_uri": record.debug_uri,
        }
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _read_all(self) -> dict[str, dict[str, str | None]]:
        if not self._path.exists():
            return {}
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}
        return {
            str(key): value for key, value in payload.items() if isinstance(value, dict)
        }


class PostgresStatusStore:
    """Status store backed by Postgres table `batch_mission_runs`."""

    def __init__(self, dsn: str) -> None:
        try:
            psycopg = importlib.import_module("psycopg")
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("psycopg is required for postgres status store") from exc

        self._psycopg = psycopg
        self._dsn = dsn
        self._ensure_schema()

    def get(self, run_key: str) -> RunStatusRecord | None:
        with self._psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT run_key, status, reason, report_uri, debug_uri
                    FROM batch_mission_runs
                    WHERE run_key = %s
                    """,
                    (run_key,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return RunStatusRecord(
                    run_key=str(row[0]),
                    status=str(row[1]),
                    reason=_as_optional_str(row[2]),
                    report_uri=_as_optional_str(row[3]),
                    debug_uri=_as_optional_str(row[4]),
                )

    def upsert(self, record: RunStatusRecord) -> None:
        with self._psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO batch_mission_runs (
                      run_key, status, reason, report_uri, debug_uri
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (run_key)
                    DO UPDATE SET
                      status = EXCLUDED.status,
                      reason = EXCLUDED.reason,
                      report_uri = EXCLUDED.report_uri,
                      debug_uri = EXCLUDED.debug_uri,
                      updated_at = NOW()
                    """,
                    (
                        record.run_key,
                        record.status,
                        record.reason,
                        record.report_uri,
                        record.debug_uri,
                    ),
                )
            conn.commit()

    def _ensure_schema(self) -> None:
        with self._psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS batch_mission_runs (
                      run_key TEXT PRIMARY KEY,
                      status TEXT NOT NULL,
                      reason TEXT NULL,
                      report_uri TEXT NULL,
                      debug_uri TEXT NULL,
                      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
            conn.commit()


def _as_optional_str(value: object) -> str | None:
    return str(value) if isinstance(value, str) else None
