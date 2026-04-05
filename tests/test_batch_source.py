from __future__ import annotations

from pathlib import Path

from rescue_ai.infrastructure.artifact_storage import S3ArtifactBackendSettings
from rescue_ai.infrastructure.s3_mission_source import (
    GLOBAL_MISSION_ID,
    S3MissionSource,
)


class _FakeS3Client:
    def __init__(self, mapping: dict[str, bytes]) -> None:
        self._mapping = mapping

    def list_objects_v2(self, **kwargs):
        prefix = str(kwargs["Prefix"])
        max_keys = kwargs.get("MaxKeys")
        keys = [key for key in self._mapping if key.startswith(prefix)]
        keys.sort()
        if isinstance(max_keys, int):
            keys = keys[:max_keys]
        return {"Contents": [{"Key": key} for key in keys]}

    def download_file(self, bucket_name: str, key: str, filename: str):
        _ = bucket_name
        target = Path(filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self._mapping[key])


class _FakeBoto3:
    def __init__(self, client: _FakeS3Client) -> None:
        self._client = client

    def client(self, *args, **kwargs):
        _ = (args, kwargs)
        return self._client


def test_s3_mission_source_marks_corrupted_images(monkeypatch) -> None:
    mapping = {
        "missions/mission-1/2026-03-01/images/frame_0001.jpg": b"\xff\xd8\xff\xd9",
        "missions/mission-1/2026-03-01/images/frame_0002.jpg": b"not-an-image",
    }
    fake_client = _FakeS3Client(mapping)
    fake_boto3 = _FakeBoto3(fake_client)

    import sys

    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    source = S3MissionSource(
        settings=S3ArtifactBackendSettings(
            endpoint="https://storage.yandexcloud.net",
            region="ru-central1",
            access_key_id="key",
            secret_access_key="secret",
            bucket="bucket",
        ),
        source_prefix="missions",
        fps=2.0,
    )
    mission_input = source.load(
        mission_id="mission-1",
        ds="2026-03-01",
    )

    assert len(mission_input.frames) == 2
    assert mission_input.frames[0].is_corrupted is False
    assert mission_input.frames[1].is_corrupted is True
    assert mission_input.source_uri == "s3://bucket/missions/mission-1/2026-03-01"


def test_s3_mission_source_global_history_up_to_ds(monkeypatch) -> None:
    mapping = {
        "missions/mission-a/2026-03-01/images/frame_0001.jpg": b"\xff\xd8\xff\xd9",
        "missions/mission-b/2026-03-02/images/frame_0001.jpg": b"\xff\xd8\xff\xd9",
        "missions/mission-c/2026-03-04/images/frame_0001.jpg": b"\xff\xd8\xff\xd9",
    }
    fake_client = _FakeS3Client(mapping)
    fake_boto3 = _FakeBoto3(fake_client)

    import sys

    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    source = S3MissionSource(
        settings=S3ArtifactBackendSettings(
            endpoint="https://storage.yandexcloud.net",
            region="ru-central1",
            access_key_id="key",
            secret_access_key="secret",
            bucket="bucket",
        ),
        source_prefix="missions",
        fps=2.0,
    )
    mission_input = source.load(mission_id=GLOBAL_MISSION_ID, ds="2026-03-02")

    assert len(mission_input.frames) == 2
    assert mission_input.frames[0].frame_id == 1
    assert mission_input.frames[1].frame_id == 2
    assert mission_input.source_uri == "s3://bucket/missions/ds<=2026-03-02"


def test_s3_mission_source_supports_ds_mission_frames_layout(monkeypatch) -> None:
    mapping = {
        "missions/2026-03-27/mission-1/frames/frame_0001.jpg": b"\xff\xd8\xff\xd9",
        "missions/2026-03-27/mission-1/frames/frame_0002.jpg": b"\xff\xd8\xff\xd9",
        "missions/2026-03-27/mission-1/report.json": b"{}",
    }
    fake_client = _FakeS3Client(mapping)
    fake_boto3 = _FakeBoto3(fake_client)

    import sys

    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    source = S3MissionSource(
        settings=S3ArtifactBackendSettings(
            endpoint="https://storage.yandexcloud.net",
            region="ru-central1",
            access_key_id="key",
            secret_access_key="secret",
            bucket="bucket",
        ),
        source_prefix="missions",
        fps=2.0,
    )
    mission_input = source.load(mission_id="mission-1", ds="2026-03-27")

    assert len(mission_input.frames) == 2
    assert mission_input.frames[0].is_corrupted is False
    assert mission_input.frames[1].is_corrupted is False
    assert mission_input.source_uri == "s3://bucket/missions/2026-03-27/mission-1"
