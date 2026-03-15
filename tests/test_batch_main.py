from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import services.batch_runner.main as batch_main
from libs.core.application.models import AlertRuleConfig


@dataclass
class _Args:
    mission_id: str = "mission-1"
    ds: str = "2026-03-01"
    model_version: str = "yolo-model"
    code_version: str = "code-v1"
    force: bool = False


def test_default_backends(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BATCH_RUNTIME_ENV", "local")
    monkeypatch.delenv("BATCH_STATUS_BACKEND", raising=False)
    monkeypatch.delenv("BATCH_ARTIFACT_BACKEND", raising=False)
    monkeypatch.setenv("BATCH_STATUS_PATH", str(tmp_path / "status" / "runs.json"))
    monkeypatch.setenv("BATCH_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    status_store = batch_main.build_status_store()
    artifact_store = batch_main.build_artifact_store()

    assert status_store.__class__.__name__ == "JsonStatusStore"
    assert artifact_store.__class__.__name__ == "LocalArtifactStore"


def test_default_backends_for_staging(monkeypatch) -> None:
    monkeypatch.setenv("BATCH_RUNTIME_ENV", "staging")

    monkeypatch.delenv("BATCH_STATUS_BACKEND", raising=False)
    monkeypatch.delenv("BATCH_ARTIFACT_BACKEND", raising=False)
    monkeypatch.delenv("BATCH_POSTGRES_DSN", raising=False)

    monkeypatch.delenv("BATCH_S3_BUCKET", raising=False)
    monkeypatch.delenv("BATCH_S3_ENDPOINT", raising=False)
    monkeypatch.delenv("BATCH_S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("BATCH_S3_SECRET_KEY", raising=False)
    monkeypatch.delenv("BATCH_S3_REGION", raising=False)
    monkeypatch.delenv("BATCH_S3_PREFIX", raising=False)

    monkeypatch.delenv("ARTIFACTS_S3_BUCKET", raising=False)
    monkeypatch.delenv("ARTIFACTS_S3_ENDPOINT", raising=False)
    monkeypatch.delenv("ARTIFACTS_S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("ARTIFACTS_S3_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("ARTIFACTS_S3_REGION", raising=False)
    monkeypatch.delenv("ARTIFACTS_S3_PREFIX", raising=False)

    with pytest.raises(ValueError):
        batch_main.build_status_store()

    with pytest.raises(ValueError):
        batch_main.build_artifact_store()


def test_build_status_store_requires_dsn(monkeypatch) -> None:
    monkeypatch.setenv("BATCH_STATUS_BACKEND", "postgres")
    monkeypatch.delenv("BATCH_POSTGRES_DSN", raising=False)
    with pytest.raises(ValueError):
        batch_main.build_status_store()


def test_build_artifact_store_supports_artifacts_s3_fallback(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeConnection:
        def __init__(
            self,
            endpoint_url: str | None = None,
            access_key: str | None = None,
            secret_key: str | None = None,
            region_name: str = "us-east-1",
        ) -> None:
            self.endpoint_url = endpoint_url
            self.access_key = access_key
            self.secret_key = secret_key
            self.region_name = region_name

        def has_credentials(self) -> bool:
            return bool(self.access_key and self.secret_key)

        def endpoint(self) -> str | None:
            return self.endpoint_url

    class _FakeS3ArtifactStore:
        Connection = _FakeConnection

        def __init__(
            self, bucket: str, prefix: str, connection: _FakeConnection
        ) -> None:
            captured["bucket"] = bucket
            captured["prefix"] = prefix
            captured["endpoint"] = connection.endpoint_url
            captured["access_key"] = connection.access_key
            captured["secret_key"] = connection.secret_key
            captured["region"] = connection.region_name

        def backend_name(self) -> str:
            return "fake-s3"

        def bucket_name(self) -> str:
            return str(captured["bucket"])

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
    monkeypatch.setenv("ARTIFACTS_S3_PREFIX", "batch")
    monkeypatch.setattr(batch_main, "S3ArtifactStore", _FakeS3ArtifactStore)

    _ = batch_main.build_artifact_store()

    assert captured["bucket"] == "bucket-a"
    assert captured["endpoint"] == "https://storage.yandexcloud.net"
    assert captured["access_key"] == "key-a"
    assert captured["secret_key"] == "secret-a"
    assert captured["region"] == "ru-central1"


def test_build_detector_yolo(monkeypatch) -> None:
    expected = SimpleNamespace(config_hash="cfg", rules=AlertRuleConfig())

    def _runtime_factory(model_version: str) -> SimpleNamespace:
        _ = model_version
        return expected

    monkeypatch.setattr(batch_main, "YoloDetectionRuntime", _runtime_factory)
    detector = batch_main.build_detector(model_version="model-x")

    assert detector is expected


def test_parse_args_smoke(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["batch-main", "--mission-id", "m", "--ds", "2026-03-01", "--force"],
    )
    args = batch_main.parse_args()

    assert isinstance(args, argparse.Namespace)
    assert args.force is True


def test_main_smoke(monkeypatch, capsys) -> None:
    class FakeDetector:
        """Detector test double used for CLI smoke test."""

        config_hash = "cfg"
        rules = AlertRuleConfig()

        def runtime_name(self) -> str:
            return "fake-detector"

        def detect(self, image_uri: str) -> list[object]:
            _ = image_uri
            return []

    class FakeRunner:
        """Runner test double returning a pre-built success payload."""

        def __init__(self) -> None:
            return

        def run(self, request):
            _ = request
            return type(
                "Result",
                (),
                {
                    "run_key": "rk",
                    "status": "completed",
                    "report_uri": "report",
                    "debug_uri": "debug",
                },
            )()

        def runner_name(self) -> str:
            return "fake-runner"

    def _parse_args() -> _Args:
        return _Args()

    def _build_detector(model_version: str) -> FakeDetector:
        _ = model_version
        return FakeDetector()

    def _build_runner(detector: FakeDetector) -> FakeRunner:
        _ = detector
        return FakeRunner()

    monkeypatch.setattr(batch_main, "parse_args", _parse_args)
    monkeypatch.setattr(batch_main, "build_detector", _build_detector)
    monkeypatch.setattr(batch_main, "build_runner", _build_runner)

    batch_main.main()

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "completed"
