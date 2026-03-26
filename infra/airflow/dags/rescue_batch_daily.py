"""Airflow DAG for the Rescue-AI unified ML pipeline.

Four chained stages run sequentially per date partition:

    data  >>  train  >>  validate  >>  inference

Each stage is idempotent — re-runs skip existing artifacts unless
the ``--force`` flag is passed via ``BATCH_FORCE`` env variable.
"""

from __future__ import annotations

import os
from datetime import timedelta

from airflow import DAG
from airflow.providers.docker.operators.docker import DockerOperator
from docker.types import Mount
from pendulum import datetime

DAG_ID = "rescue_batch_daily"
MISSION_ID = os.getenv("BATCH_MISSION_ID", "demo_mission")
CODE_VERSION = os.getenv("BATCH_CODE_VERSION", "dev")
MODEL_VERSION = os.getenv("BATCH_MODEL_VERSION", "yolov8n_baseline_multiscale")
FORCE_FLAG = "--force" if os.getenv("BATCH_FORCE", "").lower() in {"1", "true"} else ""

_COMMON_ENV = {
    "BATCH_RUNTIME_ENV": os.getenv("BATCH_RUNTIME_ENV", "local"),
    "BATCH_MISSION_ROOT": os.getenv("BATCH_MISSION_ROOT", "/opt/airflow/data/missions"),
    "BATCH_ARTIFACT_ROOT": os.getenv(
        "BATCH_ARTIFACT_ROOT", "/opt/airflow/data/artifacts"
    ),
    "BATCH_STATUS_PATH": os.getenv(
        "BATCH_STATUS_PATH", "/opt/airflow/data/status/runs.json"
    ),
    "BATCH_POSTGRES_DSN": os.getenv("BATCH_POSTGRES_DSN", ""),
    "BATCH_S3_PREFIX": os.getenv("BATCH_S3_PREFIX", "batch"),
    "ARTIFACTS_S3_ENDPOINT": os.getenv("ARTIFACTS_S3_ENDPOINT", ""),
    "ARTIFACTS_S3_BUCKET": os.getenv("ARTIFACTS_S3_BUCKET", ""),
    "ARTIFACTS_S3_PREFIX": os.getenv("ARTIFACTS_S3_PREFIX", ""),
    "ARTIFACTS_S3_ACCESS_KEY_ID": os.getenv("ARTIFACTS_S3_ACCESS_KEY_ID", ""),
    "ARTIFACTS_S3_SECRET_ACCESS_KEY": os.getenv("ARTIFACTS_S3_SECRET_ACCESS_KEY", ""),
    "ARTIFACTS_S3_REGION": os.getenv("ARTIFACTS_S3_REGION", ""),
}


def _stage_command(stage: str) -> str:
    """Build the ``uv run`` command for a given pipeline stage."""
    parts = [
        "uv run python -m rescue_ai.interfaces.cli.batch",
        f"--stage {stage}",
        f"--mission-id {MISSION_ID}",
        "--ds {{ ds }}",
        f"--model-version {MODEL_VERSION}",
        f"--code-version {CODE_VERSION}",
    ]
    if FORCE_FLAG:
        parts.append(FORCE_FLAG)
    return " ".join(parts)


with DAG(
    dag_id=DAG_ID,
    description="Rescue-AI ML pipeline: data → train → validate → inference",
    start_date=datetime(2026, 3, 1),
    schedule="@daily",
    catchup=True,
    max_active_runs=1,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
        "execution_timeout": timedelta(hours=2),
    },
    tags=["rescue-ai", "ml-pipeline", "batch", "backfill"],
) as dag:
    _docker_defaults = dict(
        image="rescue-ai-batch:local",
        docker_url="unix://var/run/docker.sock",
        api_version="auto",
        auto_remove="success",
        mount_tmp_dir=False,
        mounts=[
            Mount(
                source="airflow_shared_data",
                target="/opt/airflow/data",
                type="volume",
            )
        ],
        environment=_COMMON_ENV,
    )

    data = DockerOperator(
        task_id="stage_data",
        command=_stage_command("data"),
        **_docker_defaults,
    )

    train = DockerOperator(
        task_id="stage_train",
        command=_stage_command("train"),
        **_docker_defaults,
    )

    validate = DockerOperator(
        task_id="stage_validate",
        command=_stage_command("validate"),
        **_docker_defaults,
    )

    inference = DockerOperator(
        task_id="stage_inference",
        command=_stage_command("inference"),
        **_docker_defaults,
    )

    data >> train >> validate >> inference
