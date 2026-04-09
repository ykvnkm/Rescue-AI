"""Batch CLI entry-point tests."""

from __future__ import annotations

import argparse
import json

from rescue_ai.config import get_settings
from rescue_ai.interfaces.cli import batch as batch_main


def setup_function() -> None:
    get_settings.cache_clear()


def teardown_function() -> None:
    get_settings.cache_clear()


def test_build_source_uses_s3_prefix(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeS3MissionSource:
        def __init__(self, *, settings, source_prefix: str, fps: float) -> None:
            captured["bucket"] = settings.bucket
            captured["source_prefix"] = source_prefix
            captured["fps"] = fps

    monkeypatch.setenv("ARTIFACTS_S3_BUCKET", "bucket-a")
    monkeypatch.setenv("ARTIFACTS_S3_ACCESS_KEY_ID", "key-a")
    monkeypatch.setenv("ARTIFACTS_S3_SECRET_ACCESS_KEY", "secret-a")
    monkeypatch.setenv("ARTIFACTS_S3_PREFIX", "missions")
    monkeypatch.setattr(batch_main, "S3MissionSource", _FakeS3MissionSource)

    _ = batch_main.build_source()

    assert captured["bucket"] == "bucket-a"
    assert captured["source_prefix"] == "missions"
    assert captured["fps"] == batch_main.DEFAULT_SOURCE_FPS


def test_parse_args_smoke(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "batch",
            "--stage",
            "prepare_dataset",
            "--ds",
            "2026-04-09",
        ],
    )
    args = batch_main.parse_args()

    assert isinstance(args, argparse.Namespace)
    assert args.stage == "prepare_dataset"
    assert args.ds == "2026-04-09"


def _setup_env(monkeypatch) -> None:
    monkeypatch.setenv("ARTIFACTS_S3_BUCKET", "bucket-a")
    monkeypatch.setenv("ARTIFACTS_S3_ACCESS_KEY_ID", "key-a")
    monkeypatch.setenv("ARTIFACTS_S3_SECRET_ACCESS_KEY", "secret-a")
    monkeypatch.setenv("ARTIFACTS_S3_PREFIX", "missions")


def test_main_prepare_dataset_stage_smoke(monkeypatch, capsys) -> None:
    """Smoke test: run prepare_dataset stage end-to-end via main()."""
    _setup_env(monkeypatch)

    class _FakeStore:
        pass

    class _FakeSource:
        def load(self, mission_id: str, ds: str):
            _ = (mission_id, ds)
            return {"frames": []}

    calls: dict[str, object] = {}

    def _fake_run(store, paths, *, mission_loader):
        _ = mission_loader
        calls["store"] = store
        calls["mission_id"] = paths.mission_id
        calls["ds"] = paths.ds
        return {"stage": "prepare_dataset", "status": "completed"}

    def _fake_build_stage_store() -> _FakeStore:
        return _FakeStore()

    def _fake_build_source() -> _FakeSource:
        return _FakeSource()

    def _fake_resolve_mission_ids(args, *, client, batch_prefix):
        _ = (args, client, batch_prefix)
        return ["m"]

    def _fake_build_s3_client():
        return object()

    monkeypatch.setattr(batch_main, "build_stage_store", _fake_build_stage_store)
    monkeypatch.setattr(batch_main, "build_source", _fake_build_source)
    monkeypatch.setattr(batch_main, "_resolve_mission_ids", _fake_resolve_mission_ids)
    monkeypatch.setattr(batch_main, "_build_s3_client", _fake_build_s3_client)
    monkeypatch.setattr(batch_main, "run_prepare_dataset_stage", _fake_run)

    monkeypatch.setattr(
        "sys.argv",
        [
            "batch",
            "--stage",
            "prepare_dataset",
            "--ds",
            "2026-04-09",
        ],
    )

    batch_main.main()

    captured = capsys.readouterr().out
    json_line = captured.strip().splitlines()[-1]
    output = json.loads(json_line)
    assert output["stage"] == "prepare_dataset"
    assert output["status"] == "completed"
    assert "[prepare_dataset] status=completed" in captured
    assert calls["mission_id"] == "m"
    assert calls["ds"] == "2026-04-09"


def test_main_no_missions_logs_and_exits(monkeypatch, capsys) -> None:
    """Empty discovery → log line, no row, exit success."""
    _setup_env(monkeypatch)

    def _fake_resolve_mission_ids(args, *, client, batch_prefix):
        _ = (args, client, batch_prefix)
        return []

    def _new_object() -> object:
        return object()

    monkeypatch.setattr(batch_main, "build_stage_store", _new_object)
    monkeypatch.setattr(batch_main, "_build_s3_client", _new_object)
    monkeypatch.setattr(batch_main, "_resolve_mission_ids", _fake_resolve_mission_ids)

    monkeypatch.setattr(
        "sys.argv",
        [
            "batch",
            "--stage",
            "prepare_dataset",
            "--ds",
            "2026-04-09",
        ],
    )

    batch_main.main()

    captured = capsys.readouterr().out
    assert "no missions discovered for ds=2026-04-09" in captured
