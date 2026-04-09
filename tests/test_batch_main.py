"""Batch CLI entry-point tests."""

from __future__ import annotations

import argparse
import json

import pytest

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

    def _fake_resolve_mission_ids(args, *, client, batch_prefix, model_version):
        _ = (args, client, batch_prefix, model_version)
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

    def _fake_resolve_mission_ids(args, *, client, batch_prefix, model_version):
        _ = (args, client, batch_prefix, model_version)
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


def test_build_metrics_record_normalizes_types() -> None:
    paths = batch_main.PipelinePaths(
        prefix="batch",
        mission_id="mission-1",
        ds="2026-04-09",
        model_version="mv",
    )
    dataset = {
        "rows_total": 10.9,
        "rows_positive": 3,
        "rows_corrupted": True,  # bool must fallback to 0
        "evaluation_count": "bad",
    }
    evaluation = {
        "tp": 2,
        "tn": 4.0,
        "fp": 1,
        "fn": "bad",
        "detector_errors": 0,
        "accuracy": 0.75,
        "precision": 0.66,
        "recall": True,  # bool must fallback to 0.0
        "gt_available": True,
        "passed": "yes",
    }

    record = batch_main._build_metrics_record(
        paths=paths,
        dataset=dataset,
        evaluation=evaluation,
    )

    assert record.ds == "2026-04-09"
    assert record.mission_id == "mission-1"
    assert record.rows_total == 10
    assert record.rows_corrupted == 0
    assert record.evaluation_count == 0
    assert record.fn == 0
    assert record.recall == 0.0
    assert record.gt_available is True
    assert record.validate_passed is False


def test_build_s3_settings_requires_bucket(monkeypatch) -> None:
    monkeypatch.setenv("ARTIFACTS_S3_BUCKET", "")
    monkeypatch.setenv("ARTIFACTS_S3_ACCESS_KEY_ID", "key-a")
    monkeypatch.setenv("ARTIFACTS_S3_SECRET_ACCESS_KEY", "secret-a")
    get_settings.cache_clear()

    with pytest.raises(ValueError, match="ARTIFACTS_S3_BUCKET is required"):
        _ = batch_main._build_s3_settings()


def test_join_s3_and_has_any_keys() -> None:
    class _Client:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def list_objects_v2(self, **kwargs):
            self.calls.append(kwargs)
            return {"Contents": [{"Key": "k"}]}

    client = _Client()
    path = batch_main._join_s3("/a/", "", "b/", "/c")
    assert path == "a/b/c"
    assert batch_main._has_any_keys(client, bucket="bucket-a", prefix="pfx/")
    assert client.calls == [{"Bucket": "bucket-a", "Prefix": "pfx/", "MaxKeys": 1}]


def test_evaluation_filename_uses_slug() -> None:
    filename = batch_main._evaluation_filename("YOLO v8.1+beta")
    assert filename == "evaluation_yolo_v8_1_beta.json"


def test_list_input_missions_discovers_and_deduplicates(monkeypatch) -> None:
    _setup_env(monkeypatch)

    class _Paginator:
        def paginate(self, **_kwargs):
            return [
                {
                    "CommonPrefixes": [
                        {"Prefix": "missions/ds=2026-04-09/mission-b/"},
                        {"Prefix": "missions/ds=2026-04-09/mission-a/"},
                        {"Prefix": "missions/ds=2026-04-09/mission-a/"},
                        {"Prefix": "missions/ds=2026-04-09//"},
                    ]
                }
            ]

    class _Client:
        def get_paginator(self, name: str):
            assert name == "list_objects_v2"
            return _Paginator()

        def list_objects_v2(self, *, Bucket: str, Prefix: str, MaxKeys: int):
            _ = (Bucket, MaxKeys)
            has_frames = Prefix.endswith("mission-a/frames/")
            return {"Contents": [{"Key": "frame.jpg"}]} if has_frames else {}

    missions = batch_main._list_input_missions(_Client(), ds="2026-04-09")
    assert missions == ["mission-a"]


def test_list_output_missions_filters_by_artifact(monkeypatch) -> None:
    _setup_env(monkeypatch)

    class _Paginator:
        def paginate(self, **_kwargs):
            return [
                {
                    "CommonPrefixes": [
                        {
                            "Prefix": (
                                "missions/batch/ml_pipeline/ds=2026-04-09/" "mission=x/"
                            )
                        },
                        {
                            "Prefix": (
                                "missions/batch/ml_pipeline/ds=2026-04-09/"
                                "mission=mission-a/"
                            )
                        },
                        {
                            "Prefix": (
                                "missions/batch/ml_pipeline/ds=2026-04-09/"
                                "mission=mission-b/"
                            )
                        },
                    ]
                }
            ]

    class _Client:
        def get_paginator(self, name: str):
            assert name == "list_objects_v2"
            return _Paginator()

        def list_objects_v2(self, *, Bucket: str, Prefix: str, MaxKeys: int):
            _ = (Bucket, MaxKeys)
            return {"Contents": [{"Key": "ok"}]} if "mission-a" in Prefix else {}

    missions = batch_main._list_output_missions_with_artifact(
        _Client(),
        ds="2026-04-09",
        batch_prefix="missions/batch",
        artifact_filename="dataset.json",
    )
    assert missions == ["mission-a"]


def test_resolve_mission_ids_routes_to_stage_specific_discovery(monkeypatch) -> None:
    args = argparse.Namespace(
        stage="prepare_dataset",
        ds="2026-04-09",
        model_version="mv",
    )
    monkeypatch.setattr(
        batch_main, "_list_input_missions", lambda client, *, ds: [f"input-{ds}"]
    )
    monkeypatch.setattr(
        batch_main,
        "_list_output_missions_with_artifact",
        lambda *a, **kwargs: [f"output-{kwargs['artifact_filename']}"],
    )

    assert batch_main._resolve_mission_ids(
        args, client=object(), batch_prefix="prefix", model_version="mv"
    ) == ["input-2026-04-09"]

    args.stage = "evaluate_model"
    assert batch_main._resolve_mission_ids(
        args, client=object(), batch_prefix="prefix", model_version="mv"
    ) == ["output-dataset.json"]

    args.stage = "publish_metrics"
    assert batch_main._resolve_mission_ids(
        args, client=object(), batch_prefix="prefix", model_version="mv"
    ) == ["output-evaluation_mv.json"]


def test_main_filters_missions_by_mission_ids_csv(monkeypatch, capsys) -> None:
    _setup_env(monkeypatch)
    executed: list[str] = []

    def _fake_resolve_mission_ids(args, *, client, batch_prefix, model_version):
        _ = (args, client, batch_prefix, model_version)
        return ["mission-a", "mission-b", "mission-c"]

    def _fake_run(store, paths, *, mission_loader):
        _ = (store, mission_loader)
        executed.append(paths.mission_id)
        return {"stage": "prepare_dataset", "status": "completed"}

    def _fake_build_source() -> object:
        return object()

    monkeypatch.setattr(batch_main, "build_stage_store", object)
    monkeypatch.setattr(batch_main, "_build_s3_client", object)
    monkeypatch.setattr(batch_main, "_resolve_mission_ids", _fake_resolve_mission_ids)
    monkeypatch.setattr(batch_main, "run_prepare_dataset_stage", _fake_run)
    monkeypatch.setattr(batch_main, "build_source", _fake_build_source)
    monkeypatch.setattr(
        "sys.argv",
        [
            "batch",
            "--stage",
            "prepare_dataset",
            "--ds",
            "2026-04-09",
            "--mission-ids-csv",
            "mission-c, mission-a, missing",
        ],
    )

    batch_main.main()

    _ = capsys.readouterr()
    assert executed == ["mission-a", "mission-c"]
