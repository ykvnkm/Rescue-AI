"""Artifact storage adapter tests."""

import pytest

from rescue_ai.config import get_settings
from rescue_ai.infrastructure.artifact_storage import (
    S3ArtifactBackendSettings,
    _parse_s3_uri,
    build_s3_storage,
)


def setup_function() -> None:
    get_settings.cache_clear()


def teardown_function() -> None:
    get_settings.cache_clear()


def test_build_s3_storage_raises_without_credentials(monkeypatch) -> None:
    monkeypatch.setenv("ARTIFACTS_S3_ENDPOINT", "https://storage.yandexcloud.net")
    monkeypatch.setenv("ARTIFACTS_S3_REGION", "ru-central1")
    monkeypatch.setenv("ARTIFACTS_S3_ACCESS_KEY_ID", "")
    monkeypatch.setenv("ARTIFACTS_S3_SECRET_ACCESS_KEY", "")
    monkeypatch.setenv("ARTIFACTS_S3_BUCKET", "")

    with pytest.raises(RuntimeError):
        build_s3_storage(get_settings().storage)


def test_build_s3_storage_raises_without_bucket(monkeypatch) -> None:
    monkeypatch.setenv("ARTIFACTS_S3_ENDPOINT", "https://storage.yandexcloud.net")
    monkeypatch.setenv("ARTIFACTS_S3_REGION", "ru-central1")
    monkeypatch.setenv("ARTIFACTS_S3_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("ARTIFACTS_S3_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("ARTIFACTS_S3_BUCKET", "")

    with pytest.raises(RuntimeError):
        build_s3_storage(get_settings().storage)


def test_parse_s3_uri_variants() -> None:
    assert _parse_s3_uri("s3://bucket/path/to/file.jpg") == (
        "bucket",
        "path/to/file.jpg",
    )
    assert _parse_s3_uri("https://example.com/file.jpg") is None
    assert _parse_s3_uri("s3://bucket/") is None


def test_s3_artifact_backend_settings_ready_flag() -> None:
    full = S3ArtifactBackendSettings(
        endpoint="https://storage.yandexcloud.net",
        region="ru-central1",
        access_key_id="key",
        secret_access_key="secret",
        bucket="bucket",
    )
    assert full.ready is True

    empty = S3ArtifactBackendSettings()
    assert empty.ready is False
