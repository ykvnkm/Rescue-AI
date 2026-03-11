from pathlib import Path
from tempfile import TemporaryDirectory

from services.api_gateway.infrastructure.artifact_storage import (
    ArtifactStorageSettings,
    LocalArtifactStorage,
    build_artifact_storage,
)


def test_build_artifact_storage_falls_back_to_local_when_s3_not_configured() -> None:
    settings = ArtifactStorageSettings(
        mode="s3",
        local_root=Path("runtime/test-artifacts"),
        s3_endpoint="https://storage.yandexcloud.net",
        s3_region="ru-central1",
        s3_access_key_id=None,
        s3_secret_access_key=None,
        s3_bucket=None,
    )

    storage = build_artifact_storage(settings)

    assert isinstance(storage, LocalArtifactStorage)


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
