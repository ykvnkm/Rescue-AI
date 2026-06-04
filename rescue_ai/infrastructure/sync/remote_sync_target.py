"""Default RemoteSyncTarget: remote Postgres for DB rows + S3 for artifacts.

Used by `sync-worker` in offline mode (ADR-0007 §3, диплом §3.5.1).
Concrete delivery is split на per-entity-type handlers behind a single
dict так that adding a new entity (alert, frame_event,
trajectory_point, …) is a one-line change instead of a new branch.

S3-репликация артефактов (кадры миссии, отчёты, графики траектории,
labels.json) выполняется двумя путями в зависимости от полей
``OutboxRow``:

* ``source_s3_bucket`` + ``source_s3_key`` + ``s3_bucket`` + ``s3_key``
  → **S3-to-S3 copy** через два независимых boto3-клиента: GET object
  из локального MinIO в память пода sync-worker, PUT object в
  удалённое S3. Идемпотентно: повторный upload того же ключа в
  remote S3 просто перезаписывает объект (PUT в S3 атомарен).
* ``local_path`` + ``s3_bucket`` + ``s3_key`` (legacy) → upload файла
  с диска пода. Не используется в текущей реализации, оставлено для
  обратной совместимости со старым кодом.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import psycopg

from rescue_ai.domain.ports import OutboxRow, RemoteUnavailableError
from rescue_ai.infrastructure.postgres_connection import PostgresDatabase

DbHandler = Callable[[Any, OutboxRow], None]

# Connectivity (not per-row) failures: surfaced as RemoteUnavailableError so the
# worker idles and retries without consuming a row's attempt budget (ADR-0007).
_DB_CONN_ERRORS: tuple[type[Exception], ...] = (psycopg.OperationalError,)
try:  # boto3 ships botocore; absent only in trimmed test contexts.
    from botocore.exceptions import ConnectionError as _BotoConnError

    _S3_CONN_ERRORS: tuple[type[Exception], ...] = (_BotoConnError,)
except ImportError:  # pragma: no cover
    _S3_CONN_ERRORS = ()


class RemoteSyncTargetAdapter:
    """Sends outbox rows to remote Postgres / S3.

    Принимает ДВА S3-клиента: ``source_s3_client`` (локальный MinIO,
    откуда читаем артефакты) и ``target_s3_client`` (удалённое S3, куда
    пишем). Каждый из них — любой объект, обнажающий ``get_object`` /
    ``put_object`` (для source) и ``put_object`` / ``upload_file`` (для
    target). Это позволяет тестам подсовывать фейки без boto3.

    ``source_s3_client`` может быть ``None`` — тогда поддерживается
    только legacy-ветка с ``local_path`` и DB-репликация без S3.
    """

    def __init__(
        self,
        remote_db: PostgresDatabase,
        target_s3_client: Any,
        *,
        source_s3_client: Any | None = None,
        db_handlers: dict[str, DbHandler] | None = None,
    ) -> None:
        self._remote_db = remote_db
        self._target_s3 = target_s3_client
        self._source_s3 = source_s3_client
        self._handlers: dict[str, DbHandler] = (
            db_handlers if db_handlers is not None else dict(_DEFAULT_DB_HANDLERS)
        )

    def deliver(self, row: OutboxRow) -> None:
        # 1) S3-to-S3 copy: артефакт лежит в локальном MinIO, копируем
        # в remote S3 без shared-volume между api и sync-worker подами.
        if row.source_s3_bucket and row.source_s3_key and row.s3_bucket and row.s3_key:
            self._copy_s3_object(
                src_bucket=row.source_s3_bucket,
                src_key=row.source_s3_key,
                dst_bucket=row.s3_bucket,
                dst_key=row.s3_key,
            )
            return

        # 2) Legacy file-upload (не используется новым кодом, оставлено
        # для обратной совместимости и тестов).
        if row.s3_bucket and row.s3_key and row.local_path:
            local = Path(row.local_path)
            if not local.exists():
                raise FileNotFoundError(f"outbox local_path missing: {row.local_path}")
            try:
                self._target_s3.upload_file(str(local), row.s3_bucket, row.s3_key)
            except _S3_CONN_ERRORS as error:
                raise RemoteUnavailableError(
                    f"remote S3 unreachable: {error}"
                ) from error
            return

        # 3) DB upsert: payload_json содержит данные сущности.
        handler = self._handlers.get(row.entity_type)
        if handler is None:
            raise ValueError(f"no remote handler for entity_type={row.entity_type}")
        try:
            with self._remote_db.connect() as conn:
                # The central contour stores application tables in the ``app``
                # schema (same DDL as the station). Pin the search_path so the
                # unqualified handler SQL resolves there regardless of the
                # remote role's default — mirrors the station connection.
                with conn.cursor() as cursor:
                    cursor.execute("SET search_path TO app, public")
                handler(conn, row)
                conn.commit()
        except _DB_CONN_ERRORS as error:
            raise RemoteUnavailableError(
                f"remote Postgres unreachable: {error}"
            ) from error

    def _copy_s3_object(
        self,
        *,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
    ) -> None:
        """GET source → PUT target. Используется только при наличии
        source_s3_client (offline-профиль с локальным MinIO)."""
        if self._source_s3 is None:
            raise RuntimeError(
                "source_s3_client is required for S3-to-S3 replication; "
                "outbox row points to source_s3_bucket="
                f"{src_bucket}/{src_key}"
            )
        # get_object возвращает StreamingBody — читаем целиком в память
        # пода и передаём в put_object. Для тяжёлых артефактов (>100MB)
        # можно перейти на multipart copy через UploadId, но кадры
        # миссии (jpg, ~100KB-1MB) и отчёты (json, плоты PNG) спокойно
        # помещаются в RAM одним блоком.
        try:
            response = self._source_s3.get_object(Bucket=src_bucket, Key=src_key)
            body = response["Body"].read()
            content_type = response.get("ContentType") or "application/octet-stream"
            self._target_s3.put_object(
                Bucket=dst_bucket,
                Key=dst_key,
                Body=body,
                ContentType=content_type,
            )
        except _S3_CONN_ERRORS as error:
            raise RemoteUnavailableError(f"remote S3 unreachable: {error}") from error


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


def _upsert_alert(conn: Any, row: OutboxRow) -> None:
    payload = row.payload_json
    raw_primary = payload.get("primary_detection")
    primary = raw_primary if isinstance(raw_primary, Mapping) else {}
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO alerts (
                alert_id, mission_id, frame_id, ts_sec, image_uri,
                people_detected, primary_bbox, primary_score, primary_label,
                primary_model_name, primary_explanation, detections,
                status, reviewed_by, reviewed_at_sec, decision_reason
            )
            VALUES (
                %s, %s, %s, %s, %s, %s,
                %s::jsonb, %s, %s, %s, %s,
                %s::jsonb, %s, %s, %s, %s
            )
            ON CONFLICT (alert_id) DO UPDATE SET
                mission_id = EXCLUDED.mission_id,
                frame_id = EXCLUDED.frame_id,
                ts_sec = EXCLUDED.ts_sec,
                image_uri = EXCLUDED.image_uri,
                people_detected = EXCLUDED.people_detected,
                primary_bbox = EXCLUDED.primary_bbox,
                primary_score = EXCLUDED.primary_score,
                primary_label = EXCLUDED.primary_label,
                primary_model_name = EXCLUDED.primary_model_name,
                primary_explanation = EXCLUDED.primary_explanation,
                detections = EXCLUDED.detections,
                status = EXCLUDED.status,
                reviewed_by = EXCLUDED.reviewed_by,
                reviewed_at_sec = EXCLUDED.reviewed_at_sec,
                decision_reason = EXCLUDED.decision_reason
            """,
            (
                payload.get("alert_id"),
                payload.get("mission_id"),
                payload.get("frame_id"),
                payload.get("ts_sec"),
                payload.get("image_uri"),
                payload.get("people_detected"),
                json.dumps(primary.get("bbox")),
                primary.get("score"),
                primary.get("label"),
                primary.get("model_name"),
                primary.get("explanation"),
                json.dumps(payload.get("detections") or []),
                payload.get("status"),
                payload.get("reviewed_by"),
                payload.get("reviewed_at_sec"),
                payload.get("decision_reason"),
            ),
        )


def _upsert_frame_event(conn: Any, row: OutboxRow) -> None:
    payload = row.payload_json
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO frame_events (
                mission_id, frame_id, ts_sec, image_uri,
                gt_person_present, gt_episode_id
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (mission_id, frame_id) DO UPDATE SET
                ts_sec = EXCLUDED.ts_sec,
                image_uri = EXCLUDED.image_uri,
                gt_person_present = EXCLUDED.gt_person_present,
                gt_episode_id = EXCLUDED.gt_episode_id
            """,
            (
                payload.get("mission_id"),
                payload.get("frame_id"),
                payload.get("ts_sec"),
                payload.get("image_uri"),
                payload.get("gt_person_present"),
                payload.get("gt_episode_id"),
            ),
        )


def _upsert_trajectory_point(conn: Any, row: OutboxRow) -> None:
    payload = row.payload_json
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
                payload.get("mission_id"),
                payload.get("seq"),
                payload.get("ts_sec"),
                payload.get("frame_id"),
                payload.get("x"),
                payload.get("y"),
                payload.get("z"),
                payload.get("source"),
            ),
        )


def _insert_auto_decision(conn: Any, row: OutboxRow) -> None:
    payload = row.payload_json
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
                payload.get("decision_id"),
                payload.get("mission_id"),
                payload.get("frame_id"),
                payload.get("ts_sec"),
                payload.get("kind"),
                payload.get("reason"),
                payload.get("created_at"),
            ),
        )


def _upsert_auto_mission_config(conn: Any, row: OutboxRow) -> None:
    payload = row.payload_json
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
                payload.get("mission_id"),
                payload.get("nav_mode"),
                payload.get("detector"),
                json.dumps(payload.get("config_json") or {}),
            ),
        )


# Default DB-entity → remote-handler map. Covers every entity_type the
# offline-first repositories enqueue (offline_first_repositories.py). The
# sync-worker composition (run_service.py) uses this default; tests may
# inject a narrower map. Artifact rows (frame/report/plot/…) take the
# S3-copy branch in ``deliver`` and never reach a DB handler.
_DEFAULT_DB_HANDLERS: dict[str, DbHandler] = {
    "mission": _upsert_mission,
    "alert": _upsert_alert,
    "frame_event": _upsert_frame_event,
    "trajectory_point": _upsert_trajectory_point,
    "auto_decision": _insert_auto_decision,
    "auto_mission_config": _upsert_auto_mission_config,
}


__all__ = ["RemoteSyncTargetAdapter", "DbHandler"]
