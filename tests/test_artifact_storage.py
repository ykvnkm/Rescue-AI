from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from services.api_gateway.infrastructure.artifact_storage import (
    ArtifactStorageSettings,
    LocalArtifactStorage,
    S3ArtifactBackendSettings,
    _clean_env_value,
    _env_bool,
    _normalize_mode,
    _parse_s3_uri,
    build_artifact_storage,
)


def test_build_artifact_storage_falls_back_to_local_when_s3_has_no_credentials() -> (
    None
):
    settings = ArtifactStorageSettings(
        mode="s3",
        local_root=Path("runtime/test-artifacts"),
        s3=S3ArtifactBackendSettings(
            endpoint="https://storage.yandexcloud.net",
            region="ru-central1",
            access_key_id=None,
            secret_access_key=None,
            bucket=None,
        ),
    )

    storage = build_artifact_storage(settings)

    assert isinstance(storage, LocalArtifactStorage)


def test_build_artifact_storage_raises_when_credentials_present_but_s3_invalid() -> (
    None
):
    settings = ArtifactStorageSettings(
        mode="s3",
        local_root=Path("runtime/test-artifacts"),
        s3=S3ArtifactBackendSettings(
            endpoint="https://storage.yandexcloud.net",
            region="ru-central1",
            access_key_id="key",
            secret_access_key="secret",
            bucket=None,
        ),
    )

    with pytest.raises(RuntimeError):
        build_artifact_storage(settings)


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


def test_artifact_storage_helpers_parse_and_normalize() -> None:
    assert _normalize_mode(None) == "s3"
    assert _normalize_mode("LOCAL") == "local"
    assert _normalize_mode("invalid") == "local"

    assert _env_bool(None, default=True) is True
    assert _env_bool("false", default=True) is False
    assert _env_bool("1", default=False) is True

    assert _clean_env_value("   ") is None
    assert _clean_env_value("  value ") == "value"

    assert _parse_s3_uri("s3://bucket/path/to/file.jpg") == (
        "bucket",
        "path/to/file.jpg",
    )
    assert _parse_s3_uri("https://example.com/file.jpg") is None
    assert _parse_s3_uri("s3://bucket/") is None


def test_artifact_storage_settings_flags() -> None:
    settings = ArtifactStorageSettings(
        mode="s3",
        local_root=Path("runtime/test-artifacts"),
        s3=S3ArtifactBackendSettings(
            endpoint="https://storage.yandexcloud.net",
            region="ru-central1",
            access_key_id="key",
            secret_access_key="secret",
            bucket="bucket",
        ),
    )

    assert settings.s3.has_credentials is True
    assert settings.s3.ready is True

    no_credentials = ArtifactStorageSettings(mode="s3")
    assert no_credentials.s3.has_credentials is False
    assert no_credentials.s3.ready is False
