"""Postgres-backed implementation of the SyncOutbox port (ADR-0007 §3)."""

from __future__ import annotations

import json
from typing import Any

from rescue_ai.domain.ports import OutboxRecord, OutboxRow
from rescue_ai.infrastructure.postgres_connection import PostgresDatabase


class PostgresSyncOutboxRepository:
    """Replication outbox living in the local Postgres.

    The ``enqueue`` path supports being called inside a caller-supplied
    transaction so the outbox row commits atomically with the domain
    row that produced it. The worker side uses ``SKIP LOCKED`` to claim
    batches without blocking concurrent producers.
    """

    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    def enqueue(
        self,
        record: OutboxRecord,
        *,
        conn: Any | None = None,
    ) -> None:
        if conn is not None:
            self._insert(conn, record)
            return
        with self._db.connect() as owned:
            self._insert(owned, record)
            owned.commit()

    @staticmethod
    def _insert(conn: Any, record: OutboxRecord) -> None:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO replication_outbox (
                    entity_type,
                    entity_id,
                    operation,
                    payload_json,
                    local_path,
                    s3_bucket,
                    s3_key,
                    idempotency_key
                )
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (
                    record.entity_type,
                    record.entity_id,
                    record.operation,
                    json.dumps(dict(record.payload_json)),
                    record.local_path,
                    record.s3_bucket,
                    record.s3_key,
                    record.idempotency_key,
                ),
            )

    def claim_pending(self, batch_size: int) -> list[OutboxRow]:
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE replication_outbox
                    SET status = 'processing',
                        updated_at = NOW()
                    WHERE id IN (
                        SELECT id FROM replication_outbox
                        WHERE status = 'pending'
                        ORDER BY id
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    RETURNING
                        id,
                        entity_type,
                        entity_id,
                        operation,
                        payload_json,
                        local_path,
                        s3_bucket,
                        s3_key,
                        idempotency_key,
                        attempts
                    """,
                    (batch_size,),
                )
                rows = cursor.fetchall()
            conn.commit()
        return [_row_from_tuple(row) for row in rows]

    def mark_synced(self, outbox_id: int) -> None:
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE replication_outbox
                    SET status = 'synced',
                        last_error = NULL,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (outbox_id,),
                )
            conn.commit()

    def mark_failed(self, outbox_id: int, error: str) -> None:
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE replication_outbox
                    SET status = 'pending',
                        attempts = attempts + 1,
                        last_error = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (error[:1000], outbox_id),
                )
            conn.commit()

    def reset_stuck(self, processing_timeout_sec: float) -> int:
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE replication_outbox
                    SET status = 'pending',
                        updated_at = NOW()
                    WHERE status = 'processing'
                      AND updated_at <
                          NOW() - make_interval(secs => %s)
                    """,
                    (float(processing_timeout_sec),),
                )
                affected = cursor.rowcount
            conn.commit()
        return int(affected or 0)


def _row_from_tuple(row: tuple[Any, ...]) -> OutboxRow:
    payload = row[4]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return OutboxRow(
        id=int(row[0]),
        entity_type=str(row[1]),
        entity_id=str(row[2]),
        operation=str(row[3]),
        payload_json=payload if isinstance(payload, dict) else {},
        local_path=None if row[5] is None else str(row[5]),
        s3_bucket=None if row[6] is None else str(row[6]),
        s3_key=None if row[7] is None else str(row[7]),
        idempotency_key=str(row[8]),
        attempts=int(row[9]),
    )
