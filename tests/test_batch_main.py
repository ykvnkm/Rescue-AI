from __future__ import annotations

import argparse
import json
from dataclasses import dataclass

import pytest

import services.batch_runner.main as batch_main
from libs.core.application.models import AlertRuleConfig

# pylint: disable=protected-access,too-few-public-methods,missing-class-docstring
# pylint: disable=unnecessary-lambda


@dataclass
class _Args:
    mission_id: str = "mission-1"
    ds: str = "2026-03-01"
    model_version: str = "fake-model"
    code_version: str = "code-v1"
    force: bool = False


def test_default_backends() -> None:
    assert batch_main._default_status_backend() == "json"
    assert batch_main._default_artifact_backend() == "local"


def test_default_backends_for_staging(monkeypatch) -> None:
    monkeypatch.setenv("BATCH_RUNTIME_ENV", "staging")
    assert batch_main._default_status_backend() == "postgres"
    assert batch_main._default_artifact_backend() == "s3"


def test_build_status_store_requires_dsn(monkeypatch) -> None:
    monkeypatch.setenv("BATCH_STATUS_BACKEND", "postgres")
    monkeypatch.delenv("BATCH_POSTGRES_DSN", raising=False)
    with pytest.raises(ValueError):
        batch_main.build_status_store()


def test_build_detector_fake(monkeypatch) -> None:
    monkeypatch.setenv("BATCH_DETECTOR_BACKEND", "fake")
    detector = batch_main.build_detector(model_version="model-x")
    result = detector.detect("/tmp/frame.jpg")

    assert len(result) == 1
    assert result[0].model_name == "model-x"


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
        config_hash = "cfg"
        rules = AlertRuleConfig()

    class FakeRunner:
        def __init__(self, **_: object) -> None:
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

    monkeypatch.setattr(batch_main, "parse_args", lambda: _Args())
    monkeypatch.setattr(
        batch_main, "build_detector", lambda model_version: FakeDetector()
    )
    monkeypatch.setattr(batch_main, "MissionBatchRunner", FakeRunner)
    monkeypatch.setattr(batch_main, "build_source", lambda: object())
    monkeypatch.setattr(batch_main, "build_artifact_store", lambda: object())
    monkeypatch.setattr(batch_main, "build_status_store", lambda: object())
    monkeypatch.setattr(batch_main, "PilotMissionEngineFactory", lambda: object())

    batch_main.main()

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "completed"
