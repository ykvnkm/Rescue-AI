"""Smoke tests for batch CLI imports and DAG configuration."""

from __future__ import annotations

import importlib
from pathlib import Path


def test_batch_module_imports() -> None:
    module = importlib.import_module("rescue_ai.interfaces.cli.batch")
    assert hasattr(module, "main")
    assert module.STAGES == ("prepare_dataset", "evaluate_model", "publish_metrics")


def test_batch_dag_import_and_task_command() -> None:
    dag_path = Path("infra/airflow/dags/rescue_batch_daily.py")
    payload = dag_path.read_text(encoding="utf-8")
    assert 'DAG_ID = "rescue_batch_pipeline"' in payload
    assert "DockerOperator(" in payload
    assert "rescue_ai.interfaces.cli.batch" in payload
    assert 'task_id="prepare_dataset"' in payload
    assert 'task_id="evaluate_model"' in payload
    assert 'task_id="publish_metrics"' in payload
    # No skip-by-exists shortcuts left in the DAG: the stage command must
    # not pass --force, and there must be no force_rerun Param.
    assert "--force " not in payload
    assert "force_rerun" not in payload


def test_batch_cli_parse_args_smoke(monkeypatch) -> None:
    module = importlib.import_module("rescue_ai.interfaces.cli.batch")
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "batch",
            "--stage",
            "prepare_dataset",
            "--ds",
            "2026-04-09",
        ],
    )
    args = module.parse_args()

    assert args.ds == "2026-04-09"
    assert args.stage == "prepare_dataset"
    assert not hasattr(args, "force") or args.force is False
