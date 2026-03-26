"""Batch CLI entry-point tests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rescue_ai.config import get_settings
from rescue_ai.interfaces.cli import batch as batch_main


def test_default_backends(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BATCH_RUNTIME_ENV", "local")
    monkeypatch.delenv("BATCH_STATUS_BACKEND", raising=False)
    monkeypatch.delenv("BATCH_ARTIFACT_BACKEND", raising=False)
    monkeypatch.setenv("BATCH_STATUS_PATH", str(tmp_path / "status" / "runs.json"))
    monkeypatch.setenv("BATCH_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    status_store = batch_main.build_status_store()
    artifact_store = batch_main.build_artifact_store()

    assert status_store.__class__.__name__ == "JsonStatusStore"
    assert artifact_store.__class__.__name__ == "LocalArtifactStorage"


def test_default_backends_for_staging(monkeypatch) -> None:
    monkeypatch.setenv("BATCH_RUNTIME_ENV", "staging")
    monkeypatch.delenv("BATCH_STATUS_BACKEND", raising=False)
    monkeypatch.delenv("BATCH_ARTIFACT_BACKEND", raising=False)
    monkeypatch.delenv("BATCH_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("BATCH_S3_BUCKET", raising=False)
    monkeypatch.delenv("ARTIFACTS_S3_BUCKET", raising=False)
    get_settings.cache_clear()

    with pytest.raises(ValueError):
        batch_main.build_status_store()
    with pytest.raises(ValueError):
        batch_main.build_artifact_store()


def test_build_status_store_requires_dsn(monkeypatch) -> None:
    monkeypatch.setenv("BATCH_STATUS_BACKEND", "postgres")
    monkeypatch.delenv("BATCH_POSTGRES_DSN", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        batch_main.build_status_store()


def test_build_artifact_store_supports_artifacts_s3_fallback(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    class _FakeS3ArtifactStorage:
        """Fake S3 storage that records init arguments."""

        def __init__(self, settings, fallback_storage) -> None:
            captured["endpoint"] = settings.endpoint
            captured["bucket"] = settings.bucket
            captured["access_key_id"] = settings.access_key_id
            captured["secret_access_key"] = settings.secret_access_key
            captured["region"] = settings.region

    monkeypatch.setenv("BATCH_RUNTIME_ENV", "staging")
    monkeypatch.delenv("BATCH_ARTIFACT_BACKEND", raising=False)
    monkeypatch.delenv("BATCH_S3_BUCKET", raising=False)
    monkeypatch.delenv("BATCH_S3_ENDPOINT", raising=False)
    monkeypatch.delenv("BATCH_S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("BATCH_S3_SECRET_KEY", raising=False)
    monkeypatch.delenv("BATCH_S3_REGION", raising=False)
    monkeypatch.setenv("ARTIFACTS_S3_BUCKET", "bucket-a")
    monkeypatch.setenv("ARTIFACTS_S3_ENDPOINT", "https://storage.yandexcloud.net")
    monkeypatch.setenv("ARTIFACTS_S3_ACCESS_KEY_ID", "key-a")
    monkeypatch.setenv("ARTIFACTS_S3_SECRET_ACCESS_KEY", "secret-a")
    monkeypatch.setenv("ARTIFACTS_S3_REGION", "ru-central1")
    monkeypatch.setenv("BATCH_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    monkeypatch.setattr(batch_main, "S3ArtifactStorage", _FakeS3ArtifactStorage)

    _ = batch_main.build_artifact_store()

    assert captured["bucket"] == "bucket-a"
    assert captured["endpoint"] == "https://storage.yandexcloud.net"
    assert captured["access_key_id"] == "key-a"
    assert captured["secret_access_key"] == "secret-a"
    assert captured["region"] == "ru-central1"


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


def test_main_data_stage_smoke(monkeypatch, capsys, tmp_path: Path) -> None:
    """Smoke test: run data stage end-to-end via main()."""
    monkeypatch.setenv("BATCH_RUNTIME_ENV", "local")
    monkeypatch.setenv("BATCH_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("BATCH_STATUS_PATH", str(tmp_path / "status" / "runs.json"))
    get_settings.cache_clear()

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
