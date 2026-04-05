"""Airflow DAG for the Rescue-AI unified ML pipeline.

For each partition date:
1) data/train/validate run on the global training scope
   (all available history up to current ds);
2) inference runs per discovered mission for current ds.

Each stage is idempotent: re-runs skip existing artifacts when output
for the same key already exists.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

from airflow import DAG
from airflow.decorators import task
from airflow.providers.docker.operators.docker import DockerOperator
from docker.types import Mount
from pendulum import datetime

DAG_ID = "rescue_batch_daily"
CODE_VERSION = "main"
MODEL_VERSION = "yolov8n_baseline_multiscale"
BATCH_IMAGE = os.environ.get("BATCH_IMAGE", "rescue-ai-batch:latest")
DS_TEMPLATE = "{{ ds }}"
GLOBAL_TRAINING_MISSION_ID = "__all_missions__"
PROJECT_ROOT = os.environ.get("PROJECT_ROOT", "/home/mihask79/Rescue-AI").strip()

_ALLOWED_FRAME_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
_FRAME_DIR_NAMES = {"images", "frames"}

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
    "BATCH_VALIDATE_MAX_SAMPLES": os.environ.get("BATCH_VALIDATE_MAX_SAMPLES", "200"),
    "BATCH_VALIDATE_MIN_ACCURACY": os.environ.get(
        "BATCH_VALIDATE_MIN_ACCURACY", "0.75"
    ),
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
    if stage == "data":
        parts.append("--force")
    return " ".join(parts)


def _parse_mission_from_left(left: str) -> str | None:
    """Extract mission id from the path segment left of the images/ token."""
    left = left.rstrip("/")
    if not left:
        return None
    candidate = left.split("/")[-1]
    mission_id = (
        candidate.split("=", 1)[1] if candidate.startswith("mission=") else candidate
    )
    return mission_id.strip() or None


def _extract_mission_id_from_key(key: str, *, ds: str, root_prefix: str) -> str | None:
    """Extract mission id from S3 key for supported mission layouts."""
    normalized_root = root_prefix.strip("/")
    relative_key = key
    root_with_sep = f"{normalized_root}/" if normalized_root else ""
    if root_with_sep and key.startswith(root_with_sep):
        relative_key = key.removeprefix(root_with_sep)

    if not relative_key.lower().endswith(_ALLOWED_FRAME_EXTENSIONS):
        return None

    parts = [part for part in relative_key.split("/") if part]
    if len(parts) < 3:
        return None

    path_parts = parts[:-1]
    frame_dir_index = -1
    for idx, segment in enumerate(path_parts):
        if segment.lower() in _FRAME_DIR_NAMES:
            frame_dir_index = idx
            break
    if frame_dir_index < 0:
        return None

    mission_scope = path_parts[:frame_dir_index]
    if not mission_scope:
        return None

    def _ds_value(segment: str) -> str | None:
        if segment == ds:
            return segment
        if segment.startswith("ds="):
            value = segment.split("=", 1)[1].strip()
            return value if value == ds else None
        return None

    ds_index = next(
        (idx for idx, segment in enumerate(mission_scope) if _ds_value(segment)),
        -1,
    )
    if ds_index < 0:
        return None

    for segment in mission_scope:
        if segment.startswith("mission="):
            mission_id = segment.split("=", 1)[1].strip()
            if mission_id:
                return mission_id

    for idx in (ds_index - 1, ds_index + 1):
        if idx < 0 or idx >= len(mission_scope):
            continue
        candidate = mission_scope[idx].strip()
        if not candidate or candidate.lower() in {
            "missions",
            "images",
            "frames",
            ds.lower(),
            f"ds={ds}".lower(),
        }:
            continue
        return _parse_mission_from_left(candidate)
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
            mission_id = _extract_mission_id_from_key(key, ds=ds, root_prefix=prefix)
            if mission_id:
                found.add(mission_id)
    return sorted(found)


with DAG(
    dag_id=DAG_ID,
    description="Rescue-AI ML pipeline: global historical train + per-mission inference",
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
    _docker_defaults: dict[str, Any] = {
        "image": BATCH_IMAGE,
        "docker_url": "unix://var/run/docker.sock",
        "api_version": "auto",
        "force_pull": False,
        "auto_remove": "success",
        "mount_tmp_dir": False,
        "mounts": [
            Mount(
                source="airflow_shared_data",
                target="/opt/airflow/data",
                type="volume",
            ),
            Mount(
                source=f"{PROJECT_ROOT}/rescue_ai",
                target="/app/rescue_ai",
                type="bind",
                read_only=True,
            ),
        ],
        "environment": _COMMON_ENV,
    }

    stage_data = DockerOperator(
        task_id="stage_data",
        command=_stage_command("data", GLOBAL_TRAINING_MISSION_ID),
        **_docker_defaults,
    )

    stage_train = DockerOperator(
        task_id="stage_train",
        command=_stage_command("train", GLOBAL_TRAINING_MISSION_ID),
        **_docker_defaults,
    )

    stage_validate = DockerOperator(
        task_id="stage_validate",
        command=_stage_command("validate", GLOBAL_TRAINING_MISSION_ID),
        **_docker_defaults,
    )

    @task
    def build_inference_commands(mission_ids: list[str]) -> list[str]:
        return [_stage_command("inference", mission_id) for mission_id in mission_ids]

    discovered_missions = discover_missions(ds=DS_TEMPLATE)
    stage_data >> stage_train >> stage_validate >> discovered_missions
    inference_commands = build_inference_commands(discovered_missions)
    DockerOperator.partial(
        task_id="stage_inference",
        **_docker_defaults,
    ).expand(command=inference_commands)
