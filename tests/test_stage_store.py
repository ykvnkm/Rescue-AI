"""Unit tests for LocalStageStore and S3StageStore."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rescue_ai.infrastructure.stage_store import LocalStageStore, S3StageStore

# ── LocalStageStore ─────────────────────────────────────────────────────


def test_local_write_and_read(tmp_path: Path) -> None:
    store = LocalStageStore(tmp_path / "stages")
    store.write_json("data/manifest.json", {"frames": 42})
    result = store.read_json("data/manifest.json")
    assert result == {"frames": 42}


def test_local_exists_false_then_true(tmp_path: Path) -> None:
    store = LocalStageStore(tmp_path)
    assert store.exists("key.json") is False
    store.write_json("key.json", {"ok": True})
    assert store.exists("key.json") is True


def test_local_uri_returns_path(tmp_path: Path) -> None:
    store = LocalStageStore(tmp_path)
    assert store.uri("data/manifest.json") == str(tmp_path / "data/manifest.json")


def test_local_key_colon_replaced(tmp_path: Path) -> None:
    store = LocalStageStore(tmp_path)
    store.write_json("stage:2026-03-01.json", {"ds": "2026-03-01"})
    assert (tmp_path / "stage__2026-03-01.json").exists()
    assert store.exists("stage:2026-03-01.json")


# ── S3StageStore ────────────────────────────────────────────────────────


@pytest.fixture()
def s3_store() -> tuple[S3StageStore, MagicMock]:
    with patch("rescue_ai.infrastructure.stage_store.boto3") as mock_boto3:
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        store = S3StageStore(
            endpoint_url="https://s3.example.com",
            region_name="us-east-1",
            access_key="key",
            secret_key="secret",
            bucket="test-bucket",
        )
        return store, mock_client


def test_s3_exists_true(s3_store: tuple[S3StageStore, MagicMock]) -> None:
    store, client = s3_store
    client.head_object.return_value = {}
    assert store.exists("data/manifest.json") is True


def test_s3_exists_false_404(s3_store: tuple[S3StageStore, MagicMock]) -> None:
    store, client = s3_store
    error_response = {"ResponseMetadata": {"HTTPStatusCode": 404}, "Error": {}}
    from botocore.exceptions import ClientError

    client.head_object.side_effect = ClientError(error_response, "HeadObject")
    assert store.exists("missing.json") is False


def test_s3_write_json(s3_store: tuple[S3StageStore, MagicMock]) -> None:
    store, client = s3_store
    store.write_json("key.json", {"value": 1})
    client.put_object.assert_called_once()
    call_kwargs = client.put_object.call_args[1]
    assert call_kwargs["Bucket"] == "test-bucket"
    assert call_kwargs["Key"] == "key.json"


def test_s3_read_json(s3_store: tuple[S3StageStore, MagicMock]) -> None:
    store, client = s3_store
    body_mock = MagicMock()
    body_mock.read.return_value = b'{"frames": 10}'
    client.get_object.return_value = {"Body": body_mock}
    result = store.read_json("key.json")
    assert result == {"frames": 10}


def test_s3_uri(s3_store: tuple[S3StageStore, MagicMock]) -> None:
    store, _ = s3_store
    assert store.uri("data/manifest.json") == "s3://test-bucket/data/manifest.json"
