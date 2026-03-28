"""Postgres implementation of the SyncOutbox port."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from rescue_ai.infrastructure.postgres_connection import PostgresDatabase


class PostgresSyncOutboxRepository:
    """Postgres-backed outbox for deferred sync operations."""

    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    def enqueue(
        self,
        *,
        entity_type: str,
        entity_id: str,
        operation: str,
        idempotency_key: str,
        payload_json: dict[str, object] | None = None,
        local_path: str | None = None,
        s3_bucket: str | None = None,
        s3_key: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO sync_outbox (
                        entity_type, entity_id, operation,
                        payload_json, local_path, s3_bucket, s3_key,
                        status, idempotency_key, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                    (
                        entity_type,
                        entity_id,
                        operation,
                        json.dumps(payload_json) if payload_json else None,
                        local_path,
                        s3_bucket,
                        s3_key,
                        idempotency_key,
                        now,
                        now,
                    ),
                )
            conn.commit()

    def enqueue_in_transaction(
        self,
        conn: Any,
        *,
        entity_type: str,
        entity_id: str,
        operation: str,
        idempotency_key: str,
        payload_json: dict[str, object] | None = None,
        local_path: str | None = None,
        s3_bucket: str | None = None,
        s3_key: str | None = None,
    ) -> None:
        """Enqueue within an existing connection/transaction (no commit)."""
        now = datetime.now(timezone.utc)
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO sync_outbox (
                    entity_type, entity_id, operation,
                    payload_json, local_path, s3_bucket, s3_key,
                    status, idempotency_key, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (
                    entity_type,
                    entity_id,
                    operation,
                    json.dumps(payload_json) if payload_json else None,
                    local_path,
                    s3_bucket,
                    s3_key,
                    idempotency_key,
                    now,
                    now,
                ),
            )

    def fetch_pending(self, batch_size: int) -> list[dict[str, object]]:
        now = datetime.now(timezone.utc)
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE sync_outbox
                    SET status = 'in_progress', updated_at = %s
                    WHERE id IN (
                        SELECT id FROM sync_outbox
                        WHERE status = 'pending' AND next_retry_at <= %s
                        ORDER BY
                            CASE entity_type
                                WHEN 'mission'     THEN 0
                                WHEN 'frame_event' THEN 1
                                WHEN 'frame'       THEN 1
                                WHEN 'report'      THEN 1
                                WHEN 'alert'       THEN 2
                                WHEN 'episode'     THEN 2
                                ELSE 3
                            END,
                            next_retry_at,
                            id
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING
                        id, entity_type, entity_id, operation,
                        payload_json, local_path, s3_bucket, s3_key,
                        retry_count, idempotency_key
                    """,
                    (now, now, batch_size),
                )
                rows = cursor.fetchall()
            conn.commit()

        return [
            {
                "id": row[0],
                "entity_type": row[1],
                "entity_id": row[2],
                "operation": row[3],
                "payload_json": row[4],
                "local_path": row[5],
                "s3_bucket": row[6],
                "s3_key": row[7],
                "retry_count": row[8],
                "idempotency_key": row[9],
            }
            for row in rows
        ]

    def mark_synced(self, entry_id: int) -> None:
        now = datetime.now(timezone.utc)
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE sync_outbox
                    SET status = 'synced', updated_at = %s
                    WHERE id = %s
                    """,
                    (now, entry_id),
                )
            conn.commit()

    def mark_failed(
        self,
        entry_id: int,
        *,
        error: str,
        next_retry_at: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE sync_outbox
                    SET
                        status = 'pending',
                        retry_count = retry_count + 1,
                        last_error = %s,
                        next_retry_at = %s,
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (error, next_retry_at, now, entry_id),
                )
            conn.commit()

    def reset_stuck(self, stuck_timeout_sec: float) -> int:
        now = datetime.now(timezone.utc)
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE sync_outbox
                    SET status = 'pending', updated_at = %s
                    WHERE status = 'in_progress'
                      AND updated_at < %s - INTERVAL '1 second' * %s
                    """,
                    (now, now, stuck_timeout_sec),
                )
                count = cursor.rowcount
            conn.commit()
        return count
