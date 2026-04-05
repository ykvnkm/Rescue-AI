"""Smoke tests for batch CLI imports and DAG configuration."""

from __future__ import annotations

import importlib
from pathlib import Path


def test_batch_module_imports() -> None:
    module = importlib.import_module("rescue_ai.interfaces.cli.batch")
    assert hasattr(module, "main")


def test_batch_dag_import_and_task_command() -> None:
    dag_path = Path("infra/airflow/dags/rescue_batch_daily.py")
    payload = dag_path.read_text(encoding="utf-8")
    assert 'DAG_ID = "rescue_batch_pipeline"' in payload
    assert "DockerOperator(" in payload
    assert "rescue_ai.interfaces.cli.batch" in payload


def test_batch_cli_parse_args_smoke(monkeypatch) -> None:
    module = importlib.import_module("rescue_ai.interfaces.cli.batch")
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "batch",
            "--stage",
            "data",
            "--mission-id",
            "mission-1",
            "--ds",
            "2026-03-01",
            "--force",
        ],
    )
    args = module.parse_args()

    assert args.mission_id == "mission-1"
    assert args.ds == "2026-03-01"
    assert args.stage == "data"
    assert args.force is True
