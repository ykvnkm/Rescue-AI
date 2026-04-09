"""Tests for the canonical S3 mission source."""

from __future__ import annotations

import json
from pathlib import Path

from rescue_ai.infrastructure.artifact_storage import S3ArtifactBackendSettings
from rescue_ai.infrastructure.s3_mission_source import S3MissionSource


class _FakeS3Client:
    def __init__(self, mapping: dict[str, bytes]) -> None:
        self._mapping = mapping

    def list_objects_v2(self, **kwargs):
        prefix = str(kwargs["Prefix"])
        max_keys = kwargs.get("MaxKeys")
        keys = sorted(key for key in self._mapping if key.startswith(prefix))
        if isinstance(max_keys, int):
            keys = keys[:max_keys]
        return {"Contents": [{"Key": key} for key in keys]}

    def get_paginator(self, _name: str):
        client = self

        class _Paginator:
            def paginate(self, **kwargs):
                yield client.list_objects_v2(**kwargs)

        return _Paginator()

    def get_object(self, **kwargs):
        key = kwargs["Key"]
        if key not in self._mapping:
            raise KeyError(key)

        class _Body:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

        return {"Body": _Body(self._mapping[key])}

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


def _build_source(monkeypatch, mapping: dict[str, bytes]) -> S3MissionSource:
    fake_client = _FakeS3Client(mapping)
    fake_boto3 = _FakeBoto3(fake_client)
    import sys

    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    return S3MissionSource(
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


def test_loads_canonical_ds_partitioned_layout(monkeypatch) -> None:
    mapping = {
        "missions/2026-04-09/mission-1/frames/frame_0001.jpg": b"\xff\xd8\xff\xd9",
        "missions/2026-04-09/mission-1/frames/frame_0002.jpg": b"\xff\xd8\xff\xd9",
        "missions/2026-04-09/mission-1/labels.json": json.dumps(
            {"frame_0001.jpg": True, "frame_0002.jpg": False}
        ).encode("utf-8"),
    }
    source = _build_source(monkeypatch, mapping)
    mission_input = source.load(mission_id="mission-1", ds="2026-04-09")

    assert len(mission_input.frames) == 2
    assert mission_input.gt_available is True
    assert mission_input.frames[0].gt_person_present is True
    assert mission_input.frames[1].gt_person_present is False
    assert mission_input.source_uri == "s3://bucket/missions/2026-04-09/mission-1"


def test_marks_corrupted_image(monkeypatch) -> None:
    mapping = {
        "missions/2026-04-09/mission-1/frames/frame_0001.jpg": b"\xff\xd8\xff\xd9",
        "missions/2026-04-09/mission-1/frames/frame_0002.jpg": b"not-an-image",
    }
    source = _build_source(monkeypatch, mapping)
    mission_input = source.load(mission_id="mission-1", ds="2026-04-09")

    assert mission_input.frames[0].is_corrupted is False
    assert mission_input.frames[1].is_corrupted is True
    assert mission_input.gt_available is False  # no labels.json present


def test_missing_frames_directory_raises(monkeypatch) -> None:
    mapping: dict[str, bytes] = {}
    source = _build_source(monkeypatch, mapping)

    try:
        source.load(mission_id="mission-1", ds="2026-04-09")
    except ValueError as error:
        assert "No frame images found" in str(error)
    else:
        raise AssertionError("expected ValueError on missing frames")


def test_labels_support_nested_shape(monkeypatch) -> None:
    mapping = {
        "missions/2026-04-09/mission-1/frames/frame_0001.jpg": b"\xff\xd8\xff\xd9",
        "missions/2026-04-09/mission-1/labels.json": json.dumps(
            {"frame_0001.jpg": {"gt_person_present": True}}
        ).encode("utf-8"),
    }
    source = _build_source(monkeypatch, mapping)
    mission_input = source.load(mission_id="mission-1", ds="2026-04-09")

    assert mission_input.frames[0].gt_person_present is True


def test_labels_support_coco_shape(monkeypatch) -> None:
    mapping = {
        "missions/2026-04-09/mission-1/frames/frame_0001.jpg": b"\xff\xd8\xff\xd9",
        "missions/2026-04-09/mission-1/frames/frame_0002.jpg": b"\xff\xd8\xff\xd9",
        "missions/2026-04-09/mission-1/labels.json": json.dumps(
            {
                "images": [
                    {"id": 1, "file_name": "frame_0001.jpg"},
                    {"id": 2, "file_name": "frame_0002.jpg"},
                ],
                "annotations": [
                    {"id": 11, "image_id": 1, "category_id": 1},
                ],
                "categories": [{"id": 1, "name": "person"}],
            }
        ).encode("utf-8"),
    }
    source = _build_source(monkeypatch, mapping)
    mission_input = source.load(mission_id="mission-1", ds="2026-04-09")

    assert mission_input.frames[0].gt_person_present is True
    assert mission_input.frames[1].gt_person_present is False
