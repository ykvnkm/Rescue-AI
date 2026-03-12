from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from libs.batch.domain.models import RunStatusRecord
from libs.batch.infrastructure.artifact_store import LocalArtifactStore, S3ArtifactStore
from libs.batch.infrastructure.status_store import JsonStatusStore, PostgresStatusStore


def test_local_artifact_store_writes_report_and_debug() -> None:
    with TemporaryDirectory() as temp_dir:
        store = LocalArtifactStore(root_dir=Path(temp_dir))
        report_uri = store.write_report("m:d:c:m", {"status": "completed"})
        debug_uri = store.write_debug_rows("m:d:c:m", [{"frame_id": 1, "ok": True}])

        assert Path(report_uri).exists()
        assert Path(debug_uri).exists()
        payload = json.loads(Path(report_uri).read_text(encoding="utf-8"))
        assert payload["status"] == "completed"


def test_json_status_store_roundtrip() -> None:
    with TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "status" / "runs.json"
        store = JsonStatusStore(path=path)
        store.upsert(
            RunStatusRecord(
                run_key="k",
                status="completed",
                reason="ok",
                report_uri="r",
                debug_uri="d",
            )
        )
        record = store.get("k")

        assert record is not None
        assert record.status == "completed"
        assert record.reason == "ok"


@pytest.mark.integration
def test_postgres_status_store_roundtrip() -> None:
    dsn = os.getenv("BATCH_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("BATCH_TEST_POSTGRES_DSN is not set")

    store = PostgresStatusStore(dsn=dsn)
    store.upsert(RunStatusRecord(run_key="test-key", status="running", reason="init"))
    record = store.get("test-key")

    assert record is not None
    assert record.status == "running"


@pytest.mark.integration
def test_s3_artifact_store_roundtrip() -> None:
    endpoint = os.getenv("BATCH_TEST_S3_ENDPOINT")
    bucket = os.getenv("BATCH_TEST_S3_BUCKET")
    access_key = os.getenv("BATCH_TEST_S3_ACCESS_KEY")
    secret_key = os.getenv("BATCH_TEST_S3_SECRET_KEY")
    if not all([endpoint, bucket, access_key, secret_key]):
        pytest.skip("S3 integration env vars are not fully set")

    assert endpoint is not None
    assert bucket is not None
    assert access_key is not None
    assert secret_key is not None

    store = S3ArtifactStore(
        bucket=bucket,
        prefix="batch-integration",
        connection=S3ArtifactStore.Connection(
            endpoint_url=endpoint,
            access_key=access_key,
            secret_key=secret_key,
        ),
    )
    report_uri = store.write_report("run:key", {"status": "ok"})
    debug_uri = store.write_debug_rows("run:key", [{"frame_id": 1}])

    assert report_uri.startswith(f"s3://{bucket}/")
    assert debug_uri.startswith(f"s3://{bucket}/")
