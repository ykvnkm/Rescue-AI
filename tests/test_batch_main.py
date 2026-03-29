"""Batch CLI entry-point tests."""

from __future__ import annotations

import argparse
import json

import pytest

from rescue_ai.config import get_settings
from rescue_ai.interfaces.cli import batch as batch_main


def setup_function() -> None:
    get_settings.cache_clear()


def teardown_function() -> None:
    get_settings.cache_clear()


def test_build_status_store_requires_dsn(monkeypatch) -> None:
    monkeypatch.delenv("DB_DSN", raising=False)
    from rescue_ai.config import (
        ApiSettings,
        AppSettings,
        BatchSettings,
        DatabaseSettings,
        DetectionSettings,
        RpiSettings,
        Settings,
        StorageSettings,
    )

    empty_settings = Settings(
        app=AppSettings(),
        api=ApiSettings(),
        database=DatabaseSettings(DB_DSN=""),
        storage=StorageSettings(),
        rpi=RpiSettings(),
        batch=BatchSettings(),
        detection=DetectionSettings(),
    )
    monkeypatch.setattr(batch_main, "get_settings", lambda: empty_settings)

    with pytest.raises(ValueError, match="DB_DSN is required"):
        batch_main.build_status_store()


def test_build_artifact_store_uses_s3_settings(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeS3ArtifactStorage:
        def __init__(self, settings) -> None:
            captured["endpoint"] = settings.endpoint
            captured["bucket"] = settings.bucket
            captured["access_key_id"] = settings.access_key_id
            captured["secret_access_key"] = settings.secret_access_key
            captured["region"] = settings.region

    monkeypatch.setenv("ARTIFACTS_S3_BUCKET", "bucket-a")
    monkeypatch.setenv("ARTIFACTS_S3_ENDPOINT", "https://storage.yandexcloud.net")
    monkeypatch.setenv("ARTIFACTS_S3_ACCESS_KEY_ID", "key-a")
    monkeypatch.setenv("ARTIFACTS_S3_SECRET_ACCESS_KEY", "secret-a")
    monkeypatch.setenv("ARTIFACTS_S3_REGION", "ru-central1")
    monkeypatch.setattr(batch_main, "S3ArtifactStorage", _FakeS3ArtifactStorage)

    _ = batch_main.build_artifact_store()

    assert captured["bucket"] == "bucket-a"
    assert captured["endpoint"] == "https://storage.yandexcloud.net"
    assert captured["access_key_id"] == "key-a"
    assert captured["secret_access_key"] == "secret-a"
    assert captured["region"] == "ru-central1"


def test_build_source_uses_s3_prefix(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeS3MissionSource:
        def __init__(self, *, settings, source_prefix: str, fps: float) -> None:
            captured["bucket"] = settings.bucket
            captured["source_prefix"] = source_prefix
            captured["fps"] = fps

    monkeypatch.setenv("ARTIFACTS_S3_BUCKET", "bucket-a")
    monkeypatch.setenv("ARTIFACTS_S3_ACCESS_KEY_ID", "key-a")
    monkeypatch.setenv("ARTIFACTS_S3_SECRET_ACCESS_KEY", "secret-a")
    monkeypatch.setenv("ARTIFACTS_S3_PREFIX", "missions")
    monkeypatch.setattr(batch_main, "S3MissionSource", _FakeS3MissionSource)

    _ = batch_main.build_source()

    assert captured["bucket"] == "bucket-a"
    assert captured["source_prefix"] == "missions"
    assert captured["fps"] == batch_main.DEFAULT_SOURCE_FPS


def test_parse_args_smoke(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "batch",
            "--stage",
            "data",
            "--mission-id",
            "m",
            "--ds",
            "2026-03-01",
            "--force",
        ],
    )
    args = batch_main.parse_args()

    assert isinstance(args, argparse.Namespace)
    assert args.stage == "data"
    assert args.force is True


def test_main_data_stage_smoke(monkeypatch, capsys) -> None:
    """Smoke test: run data stage end-to-end via main()."""

    class _FakeStore:
        pass

    class _FakeSource:
        def load(self, mission_id: str, ds: str):
            _ = (mission_id, ds)
            return {"frames": []}

    calls: dict[str, object] = {}

    def _fake_run_data_stage(store, paths, *, force, mission_loader):
        _ = mission_loader
        calls["store"] = store
        calls["mission_id"] = paths.mission_id
        calls["ds"] = paths.ds
        calls["force"] = force
        return {"stage": "data", "status": "completed"}

    def _fake_build_stage_store() -> _FakeStore:
        return _FakeStore()

    def _fake_build_source() -> _FakeSource:
        return _FakeSource()

    monkeypatch.setattr(batch_main, "build_stage_store", _fake_build_stage_store)
    monkeypatch.setattr(batch_main, "build_source", _fake_build_source)
    monkeypatch.setattr(batch_main, "run_data_stage", _fake_run_data_stage)

    monkeypatch.setattr(
        "sys.argv",
        [
            "batch",
            "--stage",
            "data",
            "--mission-id",
            "m",
            "--ds",
            "2026-03-01",
        ],
    )

    batch_main.main()

    output = json.loads(capsys.readouterr().out.strip())
    assert output["stage"] == "data"
    assert output["status"] == "completed"
    assert calls["mission_id"] == "m"
    assert calls["ds"] == "2026-03-01"
    assert calls["force"] is False
