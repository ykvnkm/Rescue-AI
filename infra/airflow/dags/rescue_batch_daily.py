"""Airflow DAG for the Rescue-AI unified ML pipeline.

For each partition date the DAG auto-discovers mission datasets in S3
and runs four chained stages per mission:

    data >> train >> validate >> inference

Each stage is idempotent — re-runs skip existing artifacts when output
for the same mission/date key already exists.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

from airflow import DAG
from airflow.decorators import task, task_group
from airflow.providers.docker.operators.docker import DockerOperator
from docker.types import Mount
from pendulum import datetime

DAG_ID = "rescue_batch_daily"
CODE_VERSION = "main"
MODEL_VERSION = "yolov8n_baseline_multiscale"
BATCH_IMAGE = os.environ.get("BATCH_IMAGE", "rescue-ai-batch:latest")
DS_TEMPLATE = "{{ ds }}"

_ALLOWED_FRAME_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")

_COMMON_ENV = {
    "DB_DSN": os.environ.get("DB_DSN", ""),
    "ARTIFACTS_S3_ENDPOINT": os.environ.get("ARTIFACTS_S3_ENDPOINT", ""),
    "ARTIFACTS_S3_BUCKET": os.environ.get("ARTIFACTS_S3_BUCKET", ""),
    "ARTIFACTS_S3_PREFIX": os.environ.get("ARTIFACTS_S3_PREFIX", ""),
    "ARTIFACTS_S3_ACCESS_KEY_ID": os.environ.get("ARTIFACTS_S3_ACCESS_KEY_ID", ""),
    "ARTIFACTS_S3_SECRET_ACCESS_KEY": os.environ.get(
        "ARTIFACTS_S3_SECRET_ACCESS_KEY", ""
    ),
    "ARTIFACTS_S3_REGION": os.environ.get("ARTIFACTS_S3_REGION", ""),
}


def _stage_command(stage: str, mission_id: str) -> str:
    """Build the ``uv run`` command for a given pipeline stage."""
    parts = [
        "uv run python -m rescue_ai.interfaces.cli.batch",
        f"--stage {stage}",
        f"--mission-id {mission_id}",
        f"--ds {DS_TEMPLATE}",
        f"--model-version {MODEL_VERSION}",
        f"--code-version {CODE_VERSION}",
    ]
    return " ".join(parts)


def _extract_mission_id_from_key(key: str, *, ds: str, root_prefix: str) -> str | None:
    """Extract mission id from S3 key for both supported mission layouts."""
    normalized_root = root_prefix.strip("/")
    relative_key = key
    root_with_sep = f"{normalized_root}/" if normalized_root else ""
    if root_with_sep and key.startswith(root_with_sep):
        relative_key = key[len(root_with_sep) :]

    lower_key = relative_key.lower()
    if "/images/" not in lower_key:
        return None
    if not lower_key.endswith(_ALLOWED_FRAME_EXTENSIONS):
        return None

    partitioned_token = f"/ds={ds}/images/"
    partitioned_index = relative_key.find(partitioned_token)
    if partitioned_index >= 0:
        left = relative_key[:partitioned_index].rstrip("/")
        if not left:
            return None
        candidate = left.split("/")[-1]
        mission_id = (
            candidate.split("=", 1)[1]
            if candidate.startswith("mission=")
            else candidate
        )
        return mission_id.strip() or None

    legacy_token = f"/{ds}/images/"
    legacy_index = relative_key.find(legacy_token)
    if legacy_index >= 0:
        left = relative_key[:legacy_index].rstrip("/")
        if not left:
            return None
        candidate = left.split("/")[-1]
        mission_id = (
            candidate.split("=", 1)[1]
            if candidate.startswith("mission=")
            else candidate
        )
        return mission_id.strip() or None
    return None


@task
def discover_missions(ds: str) -> list[str]:
    """Auto-discover mission ids with frame data available for ``ds``."""
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("boto3 is required for mission discovery") from exc

    bucket = _COMMON_ENV["ARTIFACTS_S3_BUCKET"].strip()
    if not bucket:
        raise RuntimeError("ARTIFACTS_S3_BUCKET is required for mission discovery")

    prefix = _COMMON_ENV["ARTIFACTS_S3_PREFIX"].strip("/")
    search_prefix = f"{prefix}/" if prefix else ""

    client = boto3.client(
        "s3",
        endpoint_url=_COMMON_ENV["ARTIFACTS_S3_ENDPOINT"] or None,
        region_name=_COMMON_ENV["ARTIFACTS_S3_REGION"] or None,
        aws_access_key_id=_COMMON_ENV["ARTIFACTS_S3_ACCESS_KEY_ID"] or None,
        aws_secret_access_key=_COMMON_ENV["ARTIFACTS_S3_SECRET_ACCESS_KEY"] or None,
    )

    paginator = client.get_paginator("list_objects_v2")
    found: set[str] = set()
    for page in paginator.paginate(Bucket=bucket, Prefix=search_prefix):
        contents = page.get("Contents", [])
        if not isinstance(contents, list):
            continue
        for item in contents:
            if not isinstance(item, dict):
                continue
            key = item.get("Key")
            if not isinstance(key, str):
                continue
            mission_id = _extract_mission_id_from_key(
                key, ds=ds, root_prefix=prefix
            )
            if mission_id:
                found.add(mission_id)
    return sorted(found)


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
    _docker_defaults: dict[str, Any] = dict(
        image=BATCH_IMAGE,
        docker_url="unix://var/run/docker.sock",
        api_version="auto",
        force_pull=True,
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

    @task_group(group_id="mission_pipeline")
    def mission_pipeline(mission_id: str) -> None:
        data = DockerOperator(
            task_id="stage_data",
            command=_stage_command("data", mission_id),
            **_docker_defaults,
        )

        train = DockerOperator(
            task_id="stage_train",
            command=_stage_command("train", mission_id),
            **_docker_defaults,
        )

        validate = DockerOperator(
            task_id="stage_validate",
            command=_stage_command("validate", mission_id),
            **_docker_defaults,
        )

        inference = DockerOperator(
            task_id="stage_inference",
            command=_stage_command("inference", mission_id),
            **_docker_defaults,
        )

        data >> train >> validate >> inference

    mission_pipeline.expand(mission_id=discover_missions(ds=DS_TEMPLATE))
