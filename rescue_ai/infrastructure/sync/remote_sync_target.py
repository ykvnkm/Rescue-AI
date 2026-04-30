"""Default RemoteSyncTarget: remote Postgres for DB rows + S3 for artifacts.

Used by `sync-worker` in hybrid mode (ADR-0007 §3). Concrete delivery
is split into per-entity-type handlers behind a single dict so adding
a new entity (alert, frame_event, trajectory_point, …) is a one-line
change instead of a new branch.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from rescue_ai.domain.ports import OutboxRow
from rescue_ai.infrastructure.postgres_connection import PostgresDatabase

DbHandler = Callable[[Any, OutboxRow], None]


class RemoteSyncTargetAdapter:
    """Sends outbox rows to remote Postgres / S3.

    The S3 client is intentionally untyped here — the adapter accepts
    any object exposing ``upload_file(local_path, bucket, key)``,
    which lets tests use a fake without dragging boto3 in.
    """

    def __init__(
        self,
        remote_db: PostgresDatabase,
        s3_client: Any,
        *,
        db_handlers: dict[str, DbHandler] | None = None,
    ) -> None:
        self._remote_db = remote_db
        self._s3_client = s3_client
        self._handlers: dict[str, DbHandler] = db_handlers or {
            "mission": _upsert_mission,
        }

    def deliver(self, row: OutboxRow) -> None:
        if row.s3_bucket and row.s3_key and row.local_path:
            local = Path(row.local_path)
            if not local.exists():
                raise FileNotFoundError(
                    f"outbox local_path missing: {row.local_path}"
                )
            self._s3_client.upload_file(
                str(local), row.s3_bucket, row.s3_key
            )
            return

        handler = self._handlers.get(row.entity_type)
        if handler is None:
            raise ValueError(
                f"no remote handler for entity_type={row.entity_type}"
            )
        with self._remote_db.connect() as conn:
            handler(conn, row)
            conn.commit()


def _upsert_mission(conn: Any, row: OutboxRow) -> None:
    payload = row.payload_json
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
                completed_frame_id,
                slug,
                mode
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (mission_id) DO UPDATE SET
                source_name = EXCLUDED.source_name,
                status = EXCLUDED.status,
                total_frames = EXCLUDED.total_frames,
                fps = EXCLUDED.fps,
                completed_frame_id = EXCLUDED.completed_frame_id,
                slug = EXCLUDED.slug,
                mode = EXCLUDED.mode
            """,
            (
                payload.get("mission_id"),
                payload.get("source_name"),
                payload.get("status"),
                payload.get("created_at"),
                payload.get("total_frames"),
                payload.get("fps"),
                payload.get("completed_frame_id"),
                payload.get("slug"),
                payload.get("mode"),
            ),
        )


__all__ = ["RemoteSyncTargetAdapter", "DbHandler"]
