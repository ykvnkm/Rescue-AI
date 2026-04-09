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

DAG_ID = "rescue_batch_pipeline"
BATCH_IMAGE = os.environ["BATCH_IMAGE"]
NO_DATA_EXIT_CODE = 42

APP_DB_CONN_ID = "rescue_app_db"
S3_CONN_ID = "rescue_s3"

TARGET_DATE_TEMPLATE = "{{ params.run_ds | default(ds, true) }}"
MISSION_IDS_TEMPLATE = "{{ params.mission_ids_csv | default('', true) }}"

default_args = {
    "owner": "rescue-ai",
    "depends_on_past": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=15),
}

_db_conn = BaseHook.get_connection(APP_DB_CONN_ID)
_s3_conn = BaseHook.get_connection(S3_CONN_ID)
_s3_extra = _s3_conn.extra_dejson or {}

_TASK_ENV = {
    "DB_DSN": _db_conn.get_uri(),
    "ARTIFACTS_S3_ENDPOINT": _s3_extra.get("endpoint_url", ""),
    "ARTIFACTS_S3_REGION": _s3_extra.get("region_name", ""),
    "ARTIFACTS_S3_ACCESS_KEY_ID": _s3_conn.login or "",
    "ARTIFACTS_S3_SECRET_ACCESS_KEY": _s3_conn.password or "",
    "ARTIFACTS_S3_BUCKET": _s3_extra.get("bucket") or _s3_extra.get("s3_bucket") or "",
    "ARTIFACTS_S3_PREFIX": _s3_extra.get("prefix")
    or _s3_extra.get("s3_prefix")
    or "missions",
}

_COMMON: dict[str, Any] = {
    "image": BATCH_IMAGE,
    "docker_url": "unix://var/run/docker.sock",
    "api_version": "auto",
    "auto_remove": "success",
    "mount_tmp_dir": False,
    "skip_exit_code": NO_DATA_EXIT_CODE,
}

with DAG(
    dag_id=DAG_ID,
    description="Daily batch ML pipeline over mission artifacts in S3",
    default_args=default_args,
    schedule="@daily",
    start_date=datetime(2026, 4, 1),
    catchup=True,
    max_active_runs=1,
    params={
        "run_ds": Param(
            default=None,
            type=["null", "string"],
            format="date",
            description=(
                "Date to process in YYYY-MM-DD. " "Defaults to the run logical date."
            ),
        ),
        "mission_ids_csv": Param(
            default=None,
            type=["null", "string"],
            description="Optional comma-separated mission IDs allow-list.",
        ),
    },
    tags=["rescue-ai", "ml-pipeline", "batch"],
) as dag:

    prepare_dataset = DockerOperator(
        task_id="prepare_dataset",
        command=[
            "python",
            "-m",
            "rescue_ai.interfaces.cli.batch",
            "--stage",
            "prepare_dataset",
        ],
        environment={
            **_TASK_ENV,
            "BATCH_TARGET_DATE": TARGET_DATE_TEMPLATE,
            "BATCH_MISSION_IDS_CSV": MISSION_IDS_TEMPLATE,
        },
        execution_timeout=timedelta(minutes=30),
        **_COMMON,
    )

    evaluate_model = DockerOperator(
        task_id="evaluate_model",
        command=[
            "python",
            "-m",
            "rescue_ai.interfaces.cli.batch",
            "--stage",
            "evaluate_model",
        ],
        environment={
            **_TASK_ENV,
            "BATCH_TARGET_DATE": TARGET_DATE_TEMPLATE,
            "BATCH_MISSION_IDS_CSV": MISSION_IDS_TEMPLATE,
        },
        execution_timeout=timedelta(hours=1),
        **_COMMON,
    )

    publish_metrics = DockerOperator(
        task_id="publish_metrics",
        command=[
            "python",
            "-m",
            "rescue_ai.interfaces.cli.batch",
            "--stage",
            "publish_metrics",
        ],
        environment={
            **_TASK_ENV,
            "BATCH_TARGET_DATE": TARGET_DATE_TEMPLATE,
            "BATCH_MISSION_IDS_CSV": MISSION_IDS_TEMPLATE,
        },
        execution_timeout=timedelta(minutes=10),
        **_COMMON,
    )

    prepare_dataset.set_downstream(evaluate_model)
    evaluate_model.set_downstream(publish_metrics)
