"""Artifact storage adapter tests."""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from rescue_ai.config import ArtifactSettings, S3Settings, get_settings
from rescue_ai.infrastructure.s3_artifact_store import (
    LocalArtifactStorage,
    S3ArtifactBackendSettings,
    _parse_s3_uri,
    build_artifact_storage,
)


def test_build_artifact_storage_falls_back_to_local_when_s3_has_no_credentials(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ARTIFACTS_MODE", "s3")
    monkeypatch.setenv("ARTIFACTS_S3_ENDPOINT", "https://storage.yandexcloud.net")
    monkeypatch.setenv("ARTIFACTS_S3_REGION", "ru-central1")
    monkeypatch.delenv("ARTIFACTS_S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("ARTIFACTS_S3_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("ARTIFACTS_S3_BUCKET", raising=False)
    get_settings.cache_clear()

    storage = build_artifact_storage()
    assert isinstance(storage, LocalArtifactStorage)


def test_build_artifact_storage_raises_when_credentials_present_but_s3_invalid(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ARTIFACTS_MODE", "s3")
    monkeypatch.setenv("ARTIFACTS_S3_ENDPOINT", "https://storage.yandexcloud.net")
    monkeypatch.setenv("ARTIFACTS_S3_REGION", "ru-central1")
    monkeypatch.setenv("ARTIFACTS_S3_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("ARTIFACTS_S3_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("ARTIFACTS_S3_BUCKET", raising=False)
    get_settings.cache_clear()

    with pytest.raises(RuntimeError):
        build_artifact_storage()


def test_local_artifact_storage_persists_report_and_frame() -> None:
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        storage = LocalArtifactStorage(root / "artifacts")

        report_payload = {"mission_id": "mission-1", "episodes_total": 3}
        report_uri = storage.save_mission_report("mission-1", report_payload)
        loaded_report = storage.load_mission_report("mission-1")
        report_path = Path(report_uri)

        source_frame = root / "frame.jpg"
        source_frame.write_bytes(b"\xff\xd8\xff\xd9")
        stored_uri = storage.store_frame(
            mission_id="mission-1",
            frame_id=10,
            source_uri=str(source_frame),
        )
        frame_blob = storage.load_frame(stored_uri)

        assert report_path.exists()
        assert loaded_report == report_payload
        assert frame_blob is not None
        assert frame_blob.media_type.startswith("image/jpeg")
        assert frame_blob.content == b"\xff\xd8\xff\xd9"


def test_parse_s3_uri_variants() -> None:
    assert _parse_s3_uri("s3://bucket/path/to/file.jpg") == (
        "bucket",
        "path/to/file.jpg",
    )
    assert _parse_s3_uri("https://example.com/file.jpg") is None
    assert _parse_s3_uri("s3://bucket/") is None


def test_s3_artifact_backend_settings_flags() -> None:
    full = S3ArtifactBackendSettings(
        endpoint="https://storage.yandexcloud.net",
        region="ru-central1",
        access_key_id="key",
        secret_access_key="secret",
        bucket="bucket",
    )
    assert full.has_credentials is True
    assert full.ready is True

    empty = S3ArtifactBackendSettings()
    assert empty.has_credentials is False
    assert empty.ready is False


def test_s3_settings_flags() -> None:
    full = S3Settings(
        endpoint="https://storage.yandexcloud.net",
        region="ru-central1",
        access_key_id="key",
        secret_access_key="secret",
        bucket="bucket",
    )
    assert full.has_credentials is True
    assert full.ready is True

    empty = S3Settings()
    assert empty.has_credentials is False
    assert empty.ready is False
