"""Tests for the Postgres replication outbox adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rescue_ai.domain.ports import OutboxRecord
from rescue_ai.infrastructure.sync.sync_outbox_repository import (
    PostgresSyncOutboxRepository,
)


@dataclass
class _Cursor:
    rows: list[tuple[Any, ...]]
    rowcount: int = 0
    statements: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.statements.append((sql, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


@dataclass
class _Connection:
    cursor_obj: _Cursor
    commits: int = 0

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1


@dataclass
class _Db:
    conn: _Connection

    def connect(self) -> _Connection:
        return self.conn


def _repository(
    rows: list[tuple[Any, ...]] | None = None, *, rowcount: int = 0
) -> tuple[PostgresSyncOutboxRepository, _Connection]:
    cursor = _Cursor(rows or [], rowcount=rowcount)
    conn = _Connection(cursor)
    return PostgresSyncOutboxRepository(_Db(conn)), conn  # type: ignore[arg-type]


def test_enqueue_inserts_record_with_s3_source_and_commits() -> None:
    repo, conn = _repository()
    record = OutboxRecord(
        entity_type="frame",
        entity_id="m-1:1",
        operation="upload",
        payload_json={"remote_key": "frames/1.jpg"},
        idempotency_key="s3:remote:frames/1.jpg",
        s3_bucket="remote",
        s3_key="frames/1.jpg",
        source_s3_bucket="local",
        source_s3_key="frames/1.jpg",
    )

    repo.enqueue(record)

    sql, params = conn.cursor_obj.statements[0]
    assert "INSERT INTO replication_outbox" in sql
    assert params[0:3] == ("frame", "m-1:1", "upload")
    assert params[7:10] == ("local", "frames/1.jpg", "s3:remote:frames/1.jpg")
    assert conn.commits == 1


def test_enqueue_with_external_connection_does_not_commit() -> None:
    repo, conn = _repository()
    external = _Connection(_Cursor([]))

    repo.enqueue(
        OutboxRecord(
            entity_type="mission",
            entity_id="m-1",
            operation="upsert",
            payload_json={},
            idempotency_key="mission:m-1",
        ),
        conn=external,
    )

    assert external.cursor_obj.statements
    assert external.commits == 0
    assert not conn.cursor_obj.statements


def test_claim_pending_maps_rows_and_commits() -> None:
    repo, conn = _repository(
        [
            (
                7,
                "mission",
                "m-1",
                "upsert",
                '{"mission_id":"m-1"}',
                None,
                "remote",
                "key",
                "local",
                "source-key",
                "idem",
                2,
            )
        ]
    )

    rows = repo.claim_pending(10)

    assert rows[0].id == 7
    assert rows[0].payload_json == {"mission_id": "m-1"}
    assert rows[0].source_s3_bucket == "local"
    assert rows[0].attempts == 2
    assert conn.cursor_obj.statements[0][1] == (10,)
    assert conn.commits == 1


def test_status_updates_and_reset_stuck() -> None:
    repo, conn = _repository(rowcount=3)

    repo.mark_synced(5)
    repo.mark_failed(6, "x" * 1200)
    affected = repo.reset_stuck(30.5)

    statements = conn.cursor_obj.statements
    assert "SET status = 'synced'" in statements[0][0]
    assert statements[0][1] == (5,)
    assert "attempts = attempts + 1" in statements[1][0]
    assert len(statements[1][1][0]) == 1000
    assert statements[2][1] == (30.5,)
    assert affected == 3
    assert conn.commits == 3
