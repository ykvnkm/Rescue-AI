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


with DAG(
    dag_id=DAG_ID,
    description="Rescue-AI batch mission orchestration by date (idempotent + backfill)",
    start_date=datetime(2026, 3, 1),
    schedule="@daily",
    catchup=True,
    max_active_runs=1,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
        "execution_timeout": timedelta(hours=2),
    },
    tags=["rescue-ai", "batch", "backfill"],
) as dag:
    DockerOperator(
        task_id="run_batch_mission",
        image="rescue-ai-batch:local",
        docker_url="unix://var/run/docker.sock",
        api_version="auto",
        auto_remove="success",
        mount_tmp_dir=False,
        mounts=[
            Mount(source="airflow_shared_data", target="/opt/airflow/data", type="volume")
        ],
        environment={
            "BATCH_ARTIFACT_BACKEND": os.getenv("BATCH_ARTIFACT_BACKEND", "local"),
            "BATCH_STATUS_BACKEND": os.getenv("BATCH_STATUS_BACKEND", "json"),
            "BATCH_MISSION_ROOT": os.getenv("BATCH_MISSION_ROOT", "/opt/airflow/data/missions"),
            "BATCH_ARTIFACT_ROOT": os.getenv("BATCH_ARTIFACT_ROOT", "/opt/airflow/data/artifacts"),
            "BATCH_STATUS_PATH": os.getenv("BATCH_STATUS_PATH", "/opt/airflow/data/status/runs.json"),
            "BATCH_POSTGRES_DSN": os.getenv("BATCH_POSTGRES_DSN", ""),
            "BATCH_S3_ENDPOINT": os.getenv("BATCH_S3_ENDPOINT", ""),
            "BATCH_S3_BUCKET": os.getenv("BATCH_S3_BUCKET", ""),
            "BATCH_S3_PREFIX": os.getenv("BATCH_S3_PREFIX", "batch"),
            "BATCH_S3_ACCESS_KEY": os.getenv("BATCH_S3_ACCESS_KEY", ""),
            "BATCH_S3_SECRET_KEY": os.getenv("BATCH_S3_SECRET_KEY", ""),
            "BATCH_S3_REGION": os.getenv("BATCH_S3_REGION", "us-east-1"),
        },
        command=(
            "uv run python -m services.batch_runner.main "
            f"--mission-id {MISSION_ID} "
            "--ds {{ ds }} "
            f"--model-version {MODEL_VERSION} "
            f"--code-version {CODE_VERSION}"
        ),
    )
