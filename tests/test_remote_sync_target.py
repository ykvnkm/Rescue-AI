"""Tests for the remote sync target adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from rescue_ai.domain.ports import OutboxRow
from rescue_ai.infrastructure.postgres_connection import PostgresDatabase
from rescue_ai.infrastructure.sync.remote_sync_target import RemoteSyncTargetAdapter


def _row(**overrides: object) -> OutboxRow:
    data: dict[str, object] = {
        "id": 1,
        "entity_type": "mission",
        "entity_id": "m-1",
        "operation": "upsert",
        "payload_json": {"mission_id": "m-1"},
        "local_path": None,
        "s3_bucket": None,
        "s3_key": None,
        "source_s3_bucket": None,
        "source_s3_key": None,
        "idempotency_key": "k",
        "attempts": 0,
    }
    data.update(overrides)
    return OutboxRow(**data)  # type: ignore[arg-type]


@dataclass
class _SourceS3:
    objects: dict[tuple[str, str], tuple[bytes, str]]

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        body, content_type = self.objects[(Bucket, Key)]
        return {"Body": _Body(body), "ContentType": content_type}


@dataclass
class _Body:
    payload: bytes

    def read(self) -> bytes:
        return self.payload


@dataclass
class _TargetS3:
    puts: list[dict[str, object]] = field(default_factory=list)
    uploads: list[tuple[str, str, str]] = field(default_factory=list)

    def put_object(self, **kwargs: object) -> None:
        self.puts.append(dict(kwargs))

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        self.uploads.append((filename, bucket, key))


class _Cursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[object, ...] | None]] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        self.statements.append((sql, params))


class _Connection:
    def __init__(self) -> None:
        self.cursor_obj = _Cursor()
        self.commits = 0

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1


class _Db:
    def __init__(self) -> None:
        self.conn = _Connection()

    def connect(self) -> _Connection:
        return self.conn


def _postgres_db(db: _Db) -> PostgresDatabase:
    return cast(PostgresDatabase, db)


def test_s3_to_s3_copy_uses_source_and_target_clients() -> None:
    target = _TargetS3()
    adapter = RemoteSyncTargetAdapter(
        _postgres_db(_Db()),
        target,
        source_s3_client=_SourceS3({("local", "a.txt"): (b"hello", "text/plain")}),
    )

    adapter.deliver(
        _row(
            source_s3_bucket="local",
            source_s3_key="a.txt",
            s3_bucket="remote",
            s3_key="b.txt",
        )
    )

    assert target.puts == [
        {
            "Bucket": "remote",
            "Key": "b.txt",
            "Body": b"hello",
            "ContentType": "text/plain",
        }
    ]


def test_legacy_local_file_upload_and_missing_file(tmp_path: Path) -> None:
    target = _TargetS3()
    adapter = RemoteSyncTargetAdapter(_postgres_db(_Db()), target)
    artifact = tmp_path / "report.json"
    artifact.write_text("{}", encoding="utf-8")

    adapter.deliver(_row(local_path=str(artifact), s3_bucket="remote", s3_key="r.json"))
    assert target.uploads == [(str(artifact), "remote", "r.json")]

    with pytest.raises(FileNotFoundError):
        adapter.deliver(
            _row(local_path=str(tmp_path / "missing"), s3_bucket="remote", s3_key="x")
        )


def test_db_handler_dispatch_sets_search_path_and_commits() -> None:
    db = _Db()
    handled: list[OutboxRow] = []

    def _handler(conn: object, row: OutboxRow) -> None:
        assert conn is db.conn
        handled.append(row)

    adapter = RemoteSyncTargetAdapter(
        _postgres_db(db),
        _TargetS3(),
        db_handlers={"mission": _handler},
    )
    row = _row(payload_json={"mission_id": "m-1", "status": "running"})

    adapter.deliver(row)

    assert handled == [row]
    assert db.conn.commits == 1
    assert db.conn.cursor_obj.statements[0][0] == "SET search_path TO app, public"


def test_default_db_handlers_emit_expected_upsert_sql() -> None:
    rows = [
        _row(
            entity_type="mission",
            payload_json={
                "mission_id": "m-1",
                "source_name": "src",
                "status": "running",
                "created_at": "2026-06-04T00:00:00+00:00",
                "total_frames": 3,
                "fps": 1.0,
                "completed_frame_id": None,
                "slug": "slug",
                "mode": "automatic",
            },
        ),
        _row(
            entity_type="alert",
            payload_json={
                "alert_id": "a-1",
                "mission_id": "m-1",
                "frame_id": 1,
                "ts_sec": 1.0,
                "image_uri": "s3://b/k",
                "people_detected": 1,
                "primary_detection": {"bbox": [1, 2, 3, 4], "score": 0.9},
                "detections": [],
                "status": "new",
            },
        ),
        _row(
            entity_type="frame_event",
            payload_json={
                "mission_id": "m-1",
                "frame_id": 1,
                "ts_sec": 1.0,
                "image_uri": "s3://b/k",
                "gt_person_present": True,
                "gt_episode_id": "e-1",
            },
        ),
        _row(
            entity_type="trajectory_point",
            payload_json={
                "mission_id": "m-1",
                "seq": 1,
                "ts_sec": 1.0,
                "frame_id": 1,
                "x": 1.0,
                "y": 2.0,
                "z": 3.0,
                "source": "marker",
            },
        ),
        _row(
            entity_type="auto_decision",
            payload_json={
                "decision_id": "d-1",
                "mission_id": "m-1",
                "frame_id": 1,
                "ts_sec": 1.0,
                "kind": "continue",
                "reason": "ok",
                "created_at": "2026-06-04T00:00:00+00:00",
            },
        ),
        _row(
            entity_type="auto_mission_config",
            payload_json={
                "mission_id": "m-1",
                "nav_mode": "auto",
                "detector": "yolo",
                "config_json": {"origin_lat": 55.0},
            },
        ),
    ]

    for row in rows:
        db = _Db()
        adapter = RemoteSyncTargetAdapter(_postgres_db(db), _TargetS3())
        adapter.deliver(row)

        sql = "\n".join(stmt for stmt, _ in db.conn.cursor_obj.statements)
        assert "SET search_path TO app, public" in sql
        assert "ON CONFLICT" in sql
        assert db.conn.commits == 1


def test_unknown_entity_and_missing_source_client_raise() -> None:
    adapter = RemoteSyncTargetAdapter(_postgres_db(_Db()), _TargetS3(), db_handlers={})

    with pytest.raises(ValueError, match="no remote handler"):
        adapter.deliver(_row(entity_type="unknown"))

    with pytest.raises(RuntimeError, match="source_s3_client is required"):
        adapter.deliver(
            _row(
                source_s3_bucket="local",
                source_s3_key="a",
                s3_bucket="remote",
                s3_key="b",
            )
        )
