from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from rescue_ai.application.batch_dtos import RunStatusRecord
from rescue_ai.infrastructure.status_store import JsonStatusStore, PostgresStatusStore


def test_json_status_store_roundtrip() -> None:
    with TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "status" / "runs.json"
        store = JsonStatusStore(path=path)
        store.upsert(
            RunStatusRecord(
                run_key="k",
                status="completed",
                reason="ok",
                report_uri="r",
                debug_uri="d",
            )
        )
        record = store.get("k")

        assert record is not None
        assert record.status == "completed"
        assert record.reason == "ok"


@pytest.mark.integration
def test_postgres_status_store_roundtrip(pg_dsn: str) -> None:
    psycopg = pytest.importorskip("psycopg")
    root = Path(__file__).resolve().parents[1]
    schema_path = root / "infra" / "postgres" / "init" / "010-app-schema.sql"
    with psycopg.connect(pg_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(schema_path.read_text(encoding="utf-8"))
        conn.commit()

    from rescue_ai.infrastructure.postgres_connection import PostgresDatabase

    store = PostgresStatusStore(db=PostgresDatabase(dsn=pg_dsn))
    store.upsert(RunStatusRecord(run_key="test-key", status="running", reason="init"))
    record = store.get("test-key")

    assert record is not None
    assert record.status == "running"
