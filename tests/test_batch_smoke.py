from __future__ import annotations

import importlib
import sys
from pathlib import Path


def test_batch_runner_module_imports() -> None:
    module = importlib.import_module("services.batch_runner.main")
    assert hasattr(module, "main")


def test_batch_dag_import_and_task_command() -> None:
    dag_path = Path("infra/airflow/dags/idempotent_docker_backfill_demo.py")
    payload = dag_path.read_text(encoding="utf-8")
    assert 'DAG_ID = "rescue_ml_pipeline_daily"' in payload
    assert "DockerOperator(" in payload
    assert "services.batch_runner.main" in payload


def test_batch_cli_parse_args_smoke(monkeypatch) -> None:
    module = importlib.import_module("services.batch_runner.main")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "batch-main",
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
    assert args.force is True
