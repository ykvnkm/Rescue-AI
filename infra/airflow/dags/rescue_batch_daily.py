"""Daily batch DAG: prepare_dataset -> evaluate_model -> publish_metrics."""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

from airflow import DAG
from airflow.hooks.base import BaseHook
from airflow.models.param import Param
from airflow.providers.docker.operators.docker import DockerOperator
from pendulum import datetime

from rescue_ai.config import get_settings

# ── Constants ────────────────────────────────────────────────────

DAG_ID = "rescue_batch_pipeline"
BATCH_IMAGE = os.environ["BATCH_IMAGE"]
DEFAULT_MODEL_VERSION = get_settings().batch.default_model_version

APP_DB_CONN_ID = "rescue_app_db"
S3_CONN_ID = "rescue_s3"


def _build_base_env() -> dict[str, str]:
    """Compose the env dict passed into every DockerOperator.

    Reads secrets exclusively from Airflow Connections registered via the
    standard ``AIRFLOW_CONN_*`` environment variable mechanism.
    """
    db_conn = BaseHook.get_connection(APP_DB_CONN_ID)
    s3_conn = BaseHook.get_connection(S3_CONN_ID)
    s3_extra = s3_conn.extra_dejson or {}

    bucket = s3_extra.get("bucket") or s3_extra.get("s3_bucket") or ""
    prefix = s3_extra.get("prefix") or s3_extra.get("s3_prefix") or "missions"

    return {
        "DB_DSN": db_conn.get_uri(),
        "ARTIFACTS_S3_ENDPOINT": s3_extra.get("endpoint_url", ""),
        "ARTIFACTS_S3_REGION": s3_extra.get("region_name", ""),
        "ARTIFACTS_S3_ACCESS_KEY_ID": s3_conn.login or "",
        "ARTIFACTS_S3_SECRET_ACCESS_KEY": s3_conn.password or "",
        "ARTIFACTS_S3_BUCKET": bucket,
        "ARTIFACTS_S3_PREFIX": prefix,
    }


def _build_stage_command(stage: str) -> list[str]:
    """Return the templated shell command for one pipeline stage."""
    command = (
        "python -m rescue_ai.interfaces.cli.batch "
        f"--stage {stage} "
        '--ds "{{ ds }}" '
        "--mission-ids-csv "
        "\"{{ params.mission_ids_csv | default('', true) }}\" "
        '--model-version "{{ params.model_version }}"'
    )
    return ["bash", "-lc", command]


# ── DAG definition ──────────────────────────────────────────────

with DAG(
    dag_id=DAG_ID,
    description=(
        "Rescue-AI daily batch ML pipeline: "
        "prepare_dataset -> evaluate_model -> publish_metrics"
    ),
    start_date=datetime(2026, 4, 6),
    schedule="@daily",
    catchup=True,
    max_active_runs=1,
    default_args={
        "retries": 1,
        "retry_delay": timedelta(minutes=15),
    },
    params={
        "mission_ids_csv": Param(
            default=None,
            type=["null", "string"],
            description=(
                "Optional comma-separated mission IDs allow-list. "
                "Empty -> process all discovered missions for ds."
            ),
        ),
        "model_version": Param(
            default=DEFAULT_MODEL_VERSION,
            type="string",
            description="Model version tag written into artifact keys and PG rows.",
        ),
    },
    tags=["rescue-ai", "ml-pipeline", "batch"],
) as dag:
    _docker_defaults: dict[str, Any] = {
        "image": BATCH_IMAGE,
        "docker_url": "unix://var/run/docker.sock",
        "api_version": "auto",
        "force_pull": False,
        "auto_remove": "success",
        "mount_tmp_dir": False,
        "environment": _build_base_env(),
    }

    prepare_dataset = DockerOperator(
        task_id="prepare_dataset",
        execution_timeout=timedelta(minutes=30),
        command=_build_stage_command("prepare_dataset"),
        **_docker_defaults,
    )

    evaluate_model = DockerOperator(
        task_id="evaluate_model",
        execution_timeout=timedelta(hours=1),
        command=_build_stage_command("evaluate_model"),
        **_docker_defaults,
    )

    publish_metrics = DockerOperator(
        task_id="publish_metrics",
        execution_timeout=timedelta(minutes=10),
        command=_build_stage_command("publish_metrics"),
        **_docker_defaults,
    )

    prepare_dataset.set_downstream(evaluate_model)
    evaluate_model.set_downstream(publish_metrics)
