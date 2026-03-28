"""Sync worker: processes outbox entries, uploads to S3 and remote Postgres."""

from __future__ import annotations

import importlib
import json
import logging
import mimetypes
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

try:
    from psycopg.errors import ForeignKeyViolation
except ImportError:  # pragma: no cover
    ForeignKeyViolation = type("ForeignKeyViolation", (Exception,), {})

from rescue_ai.config import Settings
from rescue_ai.infrastructure.sync_outbox_repository import (
    PostgresSyncOutboxRepository,
)

logger = logging.getLogger(__name__)

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover
    boto3 = None
    BotoCoreError = Exception
    ClientError = Exception


class SyncWorker:
    """Polls outbox and syncs pending entries to S3 / remote Postgres."""

    def __init__(
        self,
        outbox: PostgresSyncOutboxRepository,
        settings: Settings,
    ) -> None:
        self._outbox = outbox
        self._sync = settings.sync
        self._storage = settings.storage
        self._s3_client: Any = None
        self._remote_conn_factory: Any = None

    def run_forever(self) -> None:
        """Main loop: poll → process → sleep."""
        logger.info(
            "Sync worker started (poll=%ss, batch=%s)",
            self._sync.poll_interval_sec,
            self._sync.batch_size,
        )
        while True:
            try:
                self._tick()
            except Exception:
                logger.exception("Sync worker tick failed")
            time.sleep(self._sync.poll_interval_sec)

    def run_once(self) -> int:
        """Single tick for testing. Returns number of processed entries."""
        return self._tick()

    def _tick(self) -> int:
        self._outbox.reset_stuck(self._sync.stuck_timeout_sec)
        entries = self._outbox.fetch_pending(self._sync.batch_size)
        if not entries:
            return 0

        processed = 0
        for entry in entries:
            try:
                self._process_entry(entry)
                self._outbox.mark_synced(int(entry["id"]))
                processed += 1
                logger.info(
                    "Synced outbox entry %s (%s/%s)",
                    entry["id"],
                    entry["operation"],
                    entry["entity_id"],
                )
            except ForeignKeyViolation as exc:
                # Parent entity not yet synced — retry quickly without
                # incrementing the backoff counter.
                quick_retry = datetime.now(timezone.utc) + timedelta(seconds=2)
                self._outbox.mark_failed(
                    int(entry["id"]),
                    error=f"ForeignKeyViolation (waiting for parent): {exc}",
                    next_retry_at=quick_retry.isoformat(),
                )
                logger.info(
                    "Outbox entry %s deferred (FK parent pending): %s/%s",
                    entry["id"],
                    entry["entity_type"],
                    entry["entity_id"],
                )
            except Exception as exc:
                retry_count = int(entry.get("retry_count", 0))
                next_retry = self._compute_next_retry(retry_count)
                self._outbox.mark_failed(
                    int(entry["id"]),
                    error=f"{type(exc).__name__}: {exc}",
                    next_retry_at=next_retry.isoformat(),
                )
                logger.warning(
                    "Outbox entry %s failed (retry=%s): %s",
                    entry["id"],
                    retry_count + 1,
                    exc,
                )
        return processed

    def _process_entry(self, entry: dict[str, object]) -> None:
        operation = str(entry["operation"])
        if operation == "upload_s3":
            self._handle_upload_s3(entry)
        elif operation == "upsert_remote_pg":
            self._handle_upsert_remote_pg(entry)
        else:
            raise ValueError(f"Unknown outbox operation: {operation}")

    def _handle_upload_s3(self, entry: dict[str, object]) -> None:
        local_path = str(entry["local_path"])
        s3_bucket = str(entry["s3_bucket"])
        s3_key = str(entry["s3_key"])

        path = Path(local_path)
        if not path.exists():
            logger.warning("Local file missing, skipping S3 upload: %s", local_path)
            return

        client = self._get_s3_client()
        content_type, _ = mimetypes.guess_type(path.name)
        client.put_object(
            Bucket=s3_bucket,
            Key=s3_key,
            Body=path.read_bytes(),
            ContentType=content_type or "application/octet-stream",
        )

    def _handle_upsert_remote_pg(self, entry: dict[str, object]) -> None:
        entity_type = str(entry["entity_type"])
        payload = entry.get("payload_json")
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid payload for upsert_remote_pg: {payload}")

        conn = self._get_remote_connection()
        try:
            if entity_type == "mission":
                self._upsert_remote_mission(conn, payload)
            elif entity_type == "alert":
                self._upsert_remote_alert(conn, payload)
            elif entity_type == "frame_event":
                self._upsert_remote_frame_event(conn, payload)
            else:
                raise ValueError(f"Unknown entity_type for remote upsert: {entity_type}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _upsert_remote_mission(self, conn: Any, payload: dict) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO missions (
                    mission_id, source_name, status, created_at,
                    total_frames, fps, completed_frame_id, slug
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (mission_id) DO UPDATE SET
                    source_name = EXCLUDED.source_name,
                    status = EXCLUDED.status,
                    total_frames = EXCLUDED.total_frames,
                    fps = EXCLUDED.fps,
                    completed_frame_id = EXCLUDED.completed_frame_id,
                    slug = EXCLUDED.slug
                """,
                (
                    payload["mission_id"],
                    payload["source_name"],
                    payload["status"],
                    payload["created_at"],
                    payload["total_frames"],
                    payload["fps"],
                    payload.get("completed_frame_id"),
                    payload.get("slug"),
                ),
            )

    def _upsert_remote_alert(self, conn: Any, payload: dict) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO alerts (
                    alert_id, mission_id, frame_id, ts_sec,
                    image_uri, people_detected,
                    primary_bbox, primary_score, primary_label,
                    primary_model_name, primary_explanation,
                    detections, status,
                    reviewed_by, reviewed_at_sec, decision_reason
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s, %s, %s, %s,
                    %s::jsonb, %s, %s, %s, %s
                )
                ON CONFLICT (alert_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    reviewed_by = EXCLUDED.reviewed_by,
                    reviewed_at_sec = EXCLUDED.reviewed_at_sec,
                    decision_reason = EXCLUDED.decision_reason
                """,
                (
                    payload["alert_id"],
                    payload["mission_id"],
                    payload["frame_id"],
                    payload["ts_sec"],
                    payload["image_uri"],
                    payload["people_detected"],
                    json.dumps(payload["primary_bbox"]),
                    payload["primary_score"],
                    payload["primary_label"],
                    payload["primary_model_name"],
                    payload.get("primary_explanation"),
                    json.dumps(payload["detections"]),
                    payload["status"],
                    payload.get("reviewed_by"),
                    payload.get("reviewed_at_sec"),
                    payload.get("decision_reason"),
                ),
            )

    def _upsert_remote_frame_event(self, conn: Any, payload: dict) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO frame_events (
                    mission_id, frame_id, ts_sec, image_uri,
                    gt_person_present, gt_episode_id
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (mission_id, frame_id) DO UPDATE SET
                    ts_sec = EXCLUDED.ts_sec,
                    image_uri = EXCLUDED.image_uri,
                    gt_person_present = EXCLUDED.gt_person_present,
                    gt_episode_id = EXCLUDED.gt_episode_id
                """,
                (
                    payload["mission_id"],
                    payload["frame_id"],
                    payload["ts_sec"],
                    payload["image_uri"],
                    payload["gt_person_present"],
                    payload.get("gt_episode_id"),
                ),
            )

    def _get_s3_client(self) -> Any:
        if self._s3_client is None:
            if boto3 is None:
                raise RuntimeError("boto3 required for S3 sync")
            self._s3_client = boto3.client(
                "s3",
                endpoint_url=self._storage.s3_endpoint or None,
                region_name=self._storage.s3_region or None,
                aws_access_key_id=self._storage.s3_access_key_id or None,
                aws_secret_access_key=self._storage.s3_secret_access_key or None,
            )
        return self._s3_client

    def _get_remote_connection(self) -> Any:
        dsn = self._sync.remote_postgres_dsn
        if not dsn:
            raise RuntimeError("SYNC_REMOTE_POSTGRES_DSN is required for remote PG sync")
        psycopg = importlib.import_module("psycopg")
        if self._remote_conn_factory is None or self._remote_conn_factory.closed:
            self._remote_conn_factory = psycopg.connect(dsn, autocommit=False)
        return self._remote_conn_factory

    def _compute_next_retry(self, retry_count: int) -> datetime:
        delay = min(
            self._sync.backoff_initial_sec * (2 ** retry_count),
            self._sync.backoff_max_sec,
        )
        return datetime.now(timezone.utc) + timedelta(seconds=delay)
