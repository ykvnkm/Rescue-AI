"""Tests for the offline-first outbox + sync worker."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import pytest

from rescue_ai.config import SyncSettings, get_settings


# ── Unit tests (no Postgres required) ────────────────────────


class TestSyncSettingsFromEnv:
    def test_defaults(self, monkeypatch) -> None:
        get_settings.cache_clear()
        monkeypatch.setenv("SYNC_ENABLED", "false")
        settings = SyncSettings()
        assert settings.enabled is False
        assert settings.poll_interval_sec == 10.0
        assert settings.batch_size == 50
        assert settings.backoff_initial_sec == 5.0
        assert settings.backoff_max_sec == 300.0

    def test_offline_first_config(self, monkeypatch) -> None:
        get_settings.cache_clear()
        monkeypatch.setenv("SYNC_ENABLED", "true")
        monkeypatch.setenv("SYNC_REMOTE_POSTGRES_DSN", "postgresql://remote/db")
        monkeypatch.setenv("SYNC_POLL_INTERVAL_SEC", "5")
        monkeypatch.setenv("SYNC_BATCH_SIZE", "100")
        settings = SyncSettings()
        assert settings.enabled is True
        assert settings.remote_postgres_dsn == "postgresql://remote/db"
        assert settings.poll_interval_sec == 5.0
        assert settings.batch_size == 100


class TestOfflineFirstArtifactStorage:
    def test_store_frame_creates_outbox_entry(self) -> None:
        from rescue_ai.infrastructure.artifact_storage import LocalArtifactStorage
        from rescue_ai.infrastructure.offline_first_storage import (
            OfflineFirstArtifactStorage,
        )

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = LocalArtifactStorage(root / "artifacts")
            outbox = MagicMock()

            storage = OfflineFirstArtifactStorage(
                local_storage=local,
                outbox=outbox,
                s3_bucket="test-bucket",
                s3_prefix="missions",
            )

            source = root / "frame.jpg"
            source.write_bytes(b"\xff\xd8\xff\xd9")

            uri = storage.store_frame("mission-1", 42, str(source))

            assert Path(uri).exists()
            outbox.enqueue.assert_called_once()
            call_kwargs = outbox.enqueue.call_args[1]
            assert call_kwargs["operation"] == "upload_s3"
            assert call_kwargs["s3_bucket"] == "test-bucket"
            assert "mission-1" in call_kwargs["s3_key"]
            assert call_kwargs["idempotency_key"] == "upload_s3:frame:mission-1:42"

    def test_save_report_creates_outbox_entry(self) -> None:
        from rescue_ai.infrastructure.artifact_storage import LocalArtifactStorage
        from rescue_ai.infrastructure.offline_first_storage import (
            OfflineFirstArtifactStorage,
        )

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = LocalArtifactStorage(root / "artifacts")
            outbox = MagicMock()

            storage = OfflineFirstArtifactStorage(
                local_storage=local,
                outbox=outbox,
                s3_bucket="test-bucket",
                s3_prefix="missions",
            )

            uri = storage.save_mission_report("m1", {"key": "val"})

            assert Path(uri).exists()
            outbox.enqueue.assert_called_once()
            call_kwargs = outbox.enqueue.call_args[1]
            assert call_kwargs["operation"] == "upload_s3"
            assert call_kwargs["idempotency_key"] == "upload_s3:report:m1"


class TestOfflineFirstRepositories:
    def test_mission_create_enqueues_outbox(self) -> None:
        from rescue_ai.domain.entities import Mission
        from rescue_ai.infrastructure.offline_first_repositories import (
            OfflineFirstMissionRepository,
        )

        inner = MagicMock()
        outbox = MagicMock()
        repo = OfflineFirstMissionRepository(inner=inner, outbox=outbox)

        mission = Mission(
            mission_id="m1",
            source_name="test",
            status="created",
            created_at="2026-01-01T00:00:00+00:00",
            total_frames=100,
            fps=6.0,
        )
        repo.create(mission)

        inner.create.assert_called_once_with(mission)
        outbox.enqueue.assert_called_once()
        kw = outbox.enqueue.call_args[1]
        assert kw["operation"] == "upsert_remote_pg"
        assert kw["entity_type"] == "mission"
        assert kw["entity_id"] == "m1"

    def test_alert_add_enqueues_outbox(self) -> None:
        from rescue_ai.domain.entities import Alert, Detection
        from rescue_ai.infrastructure.offline_first_repositories import (
            OfflineFirstAlertRepository,
        )

        inner = MagicMock()
        outbox = MagicMock()
        repo = OfflineFirstAlertRepository(inner=inner, outbox=outbox)

        alert = Alert(
            alert_id="a1",
            mission_id="m1",
            frame_id=10,
            ts_sec=1.0,
            image_uri="/tmp/frame.jpg",
            people_detected=1,
            primary_detection=Detection(
                bbox=(0.0, 0.0, 1.0, 1.0),
                score=0.9,
                label="person",
                model_name="yolo",
            ),
        )
        repo.add(alert)

        inner.add.assert_called_once_with(alert)
        outbox.enqueue.assert_called_once()
        kw = outbox.enqueue.call_args[1]
        assert kw["operation"] == "upsert_remote_pg"
        assert kw["entity_type"] == "alert"


class TestSyncWorkerUnit:
    def test_successful_s3_upload(self) -> None:
        from rescue_ai.infrastructure.sync_worker import SyncWorker

        with TemporaryDirectory() as tmp:
            local_file = Path(tmp) / "frame.jpg"
            local_file.write_bytes(b"\xff\xd8\xff\xd9")

            outbox = MagicMock()
            outbox.reset_stuck.return_value = 0
            outbox.fetch_pending.return_value = [
                {
                    "id": 1,
                    "entity_type": "frame",
                    "entity_id": "m1:42",
                    "operation": "upload_s3",
                    "payload_json": None,
                    "local_path": str(local_file),
                    "s3_bucket": "bucket",
                    "s3_key": "missions/m1/frames/42.jpg",
                    "retry_count": 0,
                    "idempotency_key": "upload_s3:frame:m1:42",
                },
            ]

            settings = MagicMock()
            settings.sync.stuck_timeout_sec = 60
            settings.sync.batch_size = 50
            settings.sync.backoff_initial_sec = 5
            settings.sync.backoff_max_sec = 300
            settings.storage.s3_endpoint = "https://s3.example.com"
            settings.storage.s3_region = "us-east-1"
            settings.storage.s3_access_key_id = "key"
            settings.storage.s3_secret_access_key = "secret"

            worker = SyncWorker(outbox=outbox, settings=settings)
            mock_client = MagicMock()
            worker._s3_client = mock_client

            processed = worker.run_once()

            assert processed == 1
            outbox.mark_synced.assert_called_once_with(1)
            mock_client.put_object.assert_called_once()

    def test_network_error_marks_failed_with_retry(self) -> None:
        from rescue_ai.infrastructure.sync_worker import SyncWorker

        with TemporaryDirectory() as tmp:
            local_file = Path(tmp) / "frame.jpg"
            local_file.write_bytes(b"\xff\xd8\xff\xd9")

            outbox = MagicMock()
            outbox.reset_stuck.return_value = 0
            outbox.fetch_pending.return_value = [
                {
                    "id": 2,
                    "entity_type": "frame",
                    "entity_id": "m1:10",
                    "operation": "upload_s3",
                    "payload_json": None,
                    "local_path": str(local_file),
                    "s3_bucket": "bucket",
                    "s3_key": "missions/m1/frames/10.jpg",
                    "retry_count": 0,
                    "idempotency_key": "upload_s3:frame:m1:10",
                },
            ]

            settings = MagicMock()
            settings.sync.stuck_timeout_sec = 60
            settings.sync.batch_size = 50
            settings.sync.backoff_initial_sec = 5
            settings.sync.backoff_max_sec = 300

            worker = SyncWorker(outbox=outbox, settings=settings)
            mock_client = MagicMock()
            mock_client.put_object.side_effect = OSError("Network unreachable")
            worker._s3_client = mock_client

            processed = worker.run_once()

            assert processed == 0
            outbox.mark_failed.assert_called_once()
            call_kwargs = outbox.mark_failed.call_args[1]
            assert "Network unreachable" in call_kwargs["error"]

    def test_duplicate_idempotency_key_no_duplicate(self) -> None:
        """After syncing, second tick returns empty batch (already synced)."""
        from rescue_ai.infrastructure.sync_worker import SyncWorker

        outbox = MagicMock()
        outbox.reset_stuck.return_value = 0
        # First call returns the entry, second call returns empty (already synced)
        outbox.fetch_pending.side_effect = [
            [
                {
                    "id": 1,
                    "entity_type": "frame",
                    "entity_id": "m1:42",
                    "operation": "upload_s3",
                    "payload_json": None,
                    "local_path": "/nonexistent",
                    "s3_bucket": "bucket",
                    "s3_key": "missions/m1/frames/42.jpg",
                    "retry_count": 0,
                    "idempotency_key": "upload_s3:frame:m1:42",
                },
            ],
            [],  # second tick: nothing pending
        ]

        settings = MagicMock()
        settings.sync.stuck_timeout_sec = 60
        settings.sync.batch_size = 50
        settings.sync.backoff_initial_sec = 5
        settings.sync.backoff_max_sec = 300

        worker = SyncWorker(outbox=outbox, settings=settings)
        mock_client = MagicMock()
        worker._s3_client = mock_client

        worker.run_once()
        outbox.mark_synced.assert_called_once_with(1)

        processed = worker.run_once()
        assert processed == 0
        assert outbox.mark_synced.call_count == 1

    def test_upsert_remote_pg_mission(self) -> None:
        from rescue_ai.infrastructure.sync_worker import SyncWorker

        outbox = MagicMock()
        outbox.reset_stuck.return_value = 0
        outbox.fetch_pending.return_value = [
            {
                "id": 3,
                "entity_type": "mission",
                "entity_id": "m1",
                "operation": "upsert_remote_pg",
                "payload_json": {
                    "mission_id": "m1",
                    "source_name": "test",
                    "status": "created",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "total_frames": 100,
                    "fps": 6.0,
                    "completed_frame_id": None,
                },
                "local_path": None,
                "s3_bucket": None,
                "s3_key": None,
                "retry_count": 0,
                "idempotency_key": "upsert_pg:mission:create:m1",
            },
        ]

        settings = MagicMock()
        settings.sync.stuck_timeout_sec = 60
        settings.sync.batch_size = 50
        settings.sync.backoff_initial_sec = 5
        settings.sync.backoff_max_sec = 300
        settings.sync.remote_postgres_dsn = "postgresql://remote/db"

        worker = SyncWorker(outbox=outbox, settings=settings)

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(worker, "_get_remote_connection", return_value=mock_conn):
            processed = worker.run_once()

        assert processed == 1
        outbox.mark_synced.assert_called_once_with(3)
        mock_cursor.execute.assert_called_once()
        sql = mock_cursor.execute.call_args[0][0]
        assert "ON CONFLICT" in sql
        assert "mission_id" in sql

    def test_foreign_key_violation_defers_with_quick_retry(self) -> None:
        """FK violation should retry quickly instead of exponential backoff."""
        from rescue_ai.infrastructure.sync_worker import SyncWorker, ForeignKeyViolation

        outbox = MagicMock()
        outbox.reset_stuck.return_value = 0
        outbox.fetch_pending.return_value = [
            {
                "id": 10,
                "entity_type": "alert",
                "entity_id": "a1",
                "operation": "upsert_remote_pg",
                "payload_json": {
                    "alert_id": "a1",
                    "mission_id": "m1",
                    "frame_id": 5,
                    "ts_sec": 1.0,
                    "image_uri": "/tmp/f.jpg",
                    "people_detected": 1,
                    "primary_bbox": [0, 0, 1, 1],
                    "primary_score": 0.9,
                    "primary_label": "person",
                    "primary_model_name": "yolo",
                    "primary_explanation": None,
                    "detections": [],
                    "status": "new",
                    "reviewed_by": None,
                    "reviewed_at_sec": None,
                    "decision_reason": None,
                },
                "local_path": None,
                "s3_bucket": None,
                "s3_key": None,
                "retry_count": 0,
                "idempotency_key": "upsert_pg:alert:a1",
            },
        ]

        settings = MagicMock()
        settings.sync.stuck_timeout_sec = 60
        settings.sync.batch_size = 50
        settings.sync.backoff_initial_sec = 5
        settings.sync.backoff_max_sec = 300
        settings.sync.remote_postgres_dsn = "postgresql://remote/db"

        worker = SyncWorker(outbox=outbox, settings=settings)

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = ForeignKeyViolation(
            "insert or update on table \"alerts\" violates foreign key constraint"
        )
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(worker, "_get_remote_connection", return_value=mock_conn):
            processed = worker.run_once()

        # Should NOT be counted as processed
        assert processed == 0
        # Should NOT be marked synced
        outbox.mark_synced.assert_not_called()
        # Should be marked failed with short retry
        outbox.mark_failed.assert_called_once()
        call_kwargs = outbox.mark_failed.call_args[1]
        assert "ForeignKeyViolation" in call_kwargs["error"]
        assert "waiting for parent" in call_kwargs["error"]

    def test_fetch_pending_orders_by_entity_priority(self) -> None:
        """Missions should be fetched before frame_events, which come before alerts."""
        # This test verifies the SQL ordering logic by checking the ORDER BY clause
        from rescue_ai.infrastructure.sync_outbox_repository import (
            PostgresSyncOutboxRepository,
        )

        db = MagicMock()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        db.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        db.connect.return_value.__exit__ = MagicMock(return_value=False)

        repo = PostgresSyncOutboxRepository(db)
        repo.fetch_pending(50)

        sql = mock_cursor.execute.call_args[0][0]
        assert "CASE entity_type" in sql
        assert "'mission'" in sql
        # mission (0) should come before alert (2)
        mission_pos = sql.index("'mission'")
        alert_pos = sql.index("'alert'")
        assert mission_pos < alert_pos
