"""Rescue-AI daily batch ML pipeline.

One DAG, runs once per day, processes every mission that has frames in
S3 through the five stages of the pipeline::

    data → train → validate → inference → publish

Design notes for defence
------------------------
* **schedule = "@daily" + catchup = True** — ``airflow dags backfill
  -s <from> -e <to> rescue_batch_pipeline`` just works. No extra DAG,
  no watcher, no cron script.
* **Idempotency** is enforced on two levels:
    1. every stage checks ``store.exists(...)`` in S3 and skips unless
       ``force=true`` (see ``rescue_ai/application/pipeline_stages.py``);
    2. the final ``publish`` stage upserts into
       ``batch_pipeline_metrics`` keyed on
       ``(ds, mission_id, model_version, code_version)`` —
       ``updated_at`` is refreshed on every re-run.
* **Secrets** come from Airflow Connections/Variables (registered via
  the standard ``AIRFLOW_CONN_*`` / ``AIRFLOW_VAR_*`` environment
  variable mechanism populated from GitHub Secrets in CI). Nothing is
  hardcoded; ``BaseHook.get_connection`` is the single source of truth.
* **conf parameters** (``dag_run.conf``):
    - ``mission_ids`` — optional list, restrict the run to specific
      missions (used for manual re-runs after data fixes);
    - ``force`` — bool, re-compute stage artifacts even if already
      present in S3;
    - ``model_version`` / ``code_version`` — label the artifacts for
      A/B experiments without editing the DAG.

S3 layout (input): ``missions/{mission_uuid}/frames/*.jpg``
S3 layout (output): ``missions/batch/ml_pipeline/mission=<uuid>/ds=<date>/*.json``
"""

from __future__ import annotations

import os
from datetime import datetime as std_datetime
from datetime import timedelta
from typing import Any, Literal, cast

_Boto3Module: Any = None
_AirflowDAGClass: Any
_TaskDecoratorFunc: Any
_BaseHookClass: Any
_ParamClass: Any
_DockerOperatorClass: Any
_MountClass: Any
_DateTimeFactory: Any

try:
    import boto3
    from airflow import DAG as AirflowDAGClass
    from airflow.decorators import task as AirflowTaskDecorator
    from airflow.hooks.base import BaseHook as AirflowBaseHookClass
    from airflow.models.param import Param as AirflowParamClass
    from airflow.providers.docker.operators.docker import (
        DockerOperator as AirflowDockerOperatorClass,
    )
    from docker.types import Mount as DockerMountClass
    from pendulum import datetime as PendulumDateTimeFactory

    _Boto3Module = boto3
    _AirflowDAGClass = AirflowDAGClass
    _TaskDecoratorFunc = AirflowTaskDecorator
    _BaseHookClass = AirflowBaseHookClass
    _ParamClass = AirflowParamClass
    _DockerOperatorClass = AirflowDockerOperatorClass
    _MountClass = DockerMountClass
    _DateTimeFactory = PendulumDateTimeFactory
except ImportError:  # pragma: no cover — keeps the file importable for linting

    class _FallbackDAG:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._args = args
            self._kwargs = kwargs

        def __enter__(self) -> "_FallbackDAG":
            return self

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Literal[False]:
            return False

    def _fallback_task(func=None, **_kwargs):
        if func is None:
            return lambda wrapped: wrapped
        return func

    class _FallbackBaseHook:
        @staticmethod
        def get_connection(_key: str) -> Any:  # noqa: D401
            class _Empty:
                login = password = host = schema = extra = ""
                extra_dejson: dict[str, str] = {}

                def get_uri(self) -> str:
                    return ""

            return _Empty()

    class _FallbackParam:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._args = args
            self._kwargs = kwargs

    class _FallbackMount:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._args = args
            self._kwargs = kwargs

    class _FallbackDockerOperator:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._args = args
            self._kwargs = kwargs

        @classmethod
        def partial(cls, *args: object, **kwargs: object) -> "_FallbackDockerOperator":
            return cls(*args, **kwargs)

        def expand(self, **_kwargs: object) -> "_FallbackDockerOperator":
            return self

        def set_upstream(self, _other: object) -> None:
            return None

    _AirflowDAGClass = cast(Any, _FallbackDAG)
    _TaskDecoratorFunc = cast(Any, _fallback_task)
    _BaseHookClass = cast(Any, _FallbackBaseHook)
    _ParamClass = cast(Any, _FallbackParam)
    _DockerOperatorClass = cast(Any, _FallbackDockerOperator)
    _MountClass = cast(Any, _FallbackMount)
    _DateTimeFactory = cast(Any, std_datetime)

boto3 = _Boto3Module
DAG = _AirflowDAGClass
task = _TaskDecoratorFunc
BaseHook = _BaseHookClass
Param = _ParamClass
DockerOperator = _DockerOperatorClass
Mount = _MountClass
datetime = _DateTimeFactory


# ── Constants ────────────────────────────────────────────────────

DAG_ID = "rescue_batch_pipeline"
BATCH_IMAGE = os.environ.get("BATCH_IMAGE", "rescue-ai-batch:local")

APP_DB_CONN_ID = "rescue_app_db"
S3_CONN_ID = "rescue_s3"

# Missions that are output artifacts, not input missions.
_EXCLUDE_PREFIXES = frozenset({"batch"})


# ── Secret wiring via Airflow Connections (standard mechanism) ──


def _build_base_env() -> dict[str, str]:
    """Compose the env dict passed into every DockerOperator.

    Reads secrets exclusively from Airflow Connections registered via the
    standard ``AIRFLOW_CONN_*`` environment variable mechanism. No secrets
    are read from the DAG's own ``os.environ``.
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


# ── Mission discovery ───────────────────────────────────────────


def _list_missions_from_s3(env: dict[str, str]) -> list[str]:
    """List mission UUIDs from S3 via CommonPrefixes (fast, no full scan)."""
    if boto3 is None:  # pragma: no cover
        raise RuntimeError("boto3 is not available")

    client = boto3.client(
        "s3",
        endpoint_url=env["ARTIFACTS_S3_ENDPOINT"] or None,
        region_name=env["ARTIFACTS_S3_REGION"] or None,
        aws_access_key_id=env["ARTIFACTS_S3_ACCESS_KEY_ID"] or None,
        aws_secret_access_key=env["ARTIFACTS_S3_SECRET_ACCESS_KEY"] or None,
    )
    bucket = env["ARTIFACTS_S3_BUCKET"]
    prefix = env["ARTIFACTS_S3_PREFIX"].strip("/")
    search_prefix = f"{prefix}/" if prefix else ""

    paginator = client.get_paginator("list_objects_v2")
    found: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=search_prefix, Delimiter="/"):
        for common in page.get("CommonPrefixes", []) or []:
            segment = common["Prefix"].rstrip("/").split("/")[-1]
            if segment and segment not in _EXCLUDE_PREFIXES:
                found.append(segment)
    return sorted(set(found))


# ── DAG definition ──────────────────────────────────────────────

with DAG(
    dag_id=DAG_ID,
    description="Rescue-AI daily batch: data → train → validate → inference → publish",
    start_date=datetime(2026, 3, 1),
    schedule="@daily",
    catchup=True,
    max_active_runs=1,
    default_args={
        "retries": 1,
        "retry_delay": timedelta(minutes=15),
    },
    params={
        "mission_ids": Param(
            default=[],
            type=["array", "null"],
            description=(
                "Optional allow-list of mission UUIDs. "
                "Empty → process every mission discovered in S3."
            ),
        ),
        "force": Param(
            default=False,
            type="boolean",
            description=(
                "Re-compute every stage even if the S3 artifact already exists."
            ),
        ),
        "model_version": Param(
            default="yolov8n_baseline_multiscale",
            type="string",
            description="Model version tag written into artifact keys and PG rows.",
        ),
        "code_version": Param(
            default="main",
            type="string",
            description="Code version tag written into artifact keys and PG rows.",
        ),
    },
    tags=["rescue-ai", "ml-pipeline", "batch"],
) as dag:

    # -- 1. Resolve the list of missions for this DAG run ------------

    @task
    def resolve_missions(**context) -> list[str]:
        """Return the sorted list of mission UUIDs to process for this run."""
        env = _build_base_env()
        conf_ids = (context["params"] or {}).get("mission_ids") or []
        discovered = _list_missions_from_s3(env)

        if conf_ids:
            requested = {str(m) for m in conf_ids}
            missing = requested - set(discovered)
            if missing:
                raise ValueError(
                    "Requested missions not found in S3: " + ", ".join(sorted(missing))
                )
            return sorted(requested)

        if not discovered:
            raise ValueError(
                "No missions discovered in S3. "
                "Upload a mission under "
                f"s3://{env['ARTIFACTS_S3_BUCKET']}/{env['ARTIFACTS_S3_PREFIX']}/"
                " or trigger the DAG with {'mission_ids': ['<uuid>']}."
            )
        return discovered

    # -- 2. Build one Docker command per (mission, stage) ------------

    @task
    def build_commands(
        mission_ids: Any,
        stage: str,
        **context,
    ) -> list[list[str]]:
        """Return a list of argv lists, one per mission, for a given stage."""
        params = context["params"] or {}
        ds = context["ds"]
        model_version = params.get("model_version", "yolov8n_baseline_multiscale")
        code_version = params.get("code_version", "main")
        force = bool(params.get("force", False))

        commands: list[list[str]] = []
        for mission_id in cast(list[str], mission_ids):
            cmd = [
                "python",
                "-m",
                "rescue_ai.interfaces.cli.batch",
                "--stage",
                stage,
                "--mission-id",
                mission_id,
                "--ds",
                ds,
                "--model-version",
                model_version,
                "--code-version",
                code_version,
            ]
            if force:
                cmd.append("--force")
            commands.append(cmd)
        return commands

    # -- 3. Shared DockerOperator defaults ---------------------------

    _docker_defaults: dict[str, object] = {
        "image": BATCH_IMAGE,
        "docker_url": "unix://var/run/docker.sock",
        "api_version": "auto",
        "force_pull": False,
        "auto_remove": "success",
        "mount_tmp_dir": False,
        "environment": _build_base_env(),
        "mounts": [
            Mount(
                source="airflow_shared_data",
                target="/opt/airflow/data",
                type="volume",
            )
        ],
    }

    # -- 4. Stage fan-out via dynamic task mapping -------------------

    missions = resolve_missions()

    data_cmds = build_commands.override(task_id="build_data_cmds")(
        mission_ids=missions, stage="data"
    )
    train_cmds = build_commands.override(task_id="build_train_cmds")(
        mission_ids=missions, stage="train"
    )
    validate_cmds = build_commands.override(task_id="build_validate_cmds")(
        mission_ids=missions, stage="validate"
    )
    inference_cmds = build_commands.override(task_id="build_inference_cmds")(
        mission_ids=missions, stage="inference"
    )
    publish_cmds = build_commands.override(task_id="build_publish_cmds")(
        mission_ids=missions, stage="publish"
    )

    stage_data = DockerOperator.partial(
        task_id="data",
        execution_timeout=timedelta(minutes=30),
        **_docker_defaults,
    ).expand(command=data_cmds)

    stage_train = DockerOperator.partial(
        task_id="train",
        execution_timeout=timedelta(hours=2),
        **_docker_defaults,
    ).expand(command=train_cmds)

    stage_validate = DockerOperator.partial(
        task_id="validate",
        execution_timeout=timedelta(hours=1),
        **_docker_defaults,
    ).expand(command=validate_cmds)

    stage_inference = DockerOperator.partial(
        task_id="inference",
        execution_timeout=timedelta(hours=6),
        **_docker_defaults,
    ).expand(command=inference_cmds)

    stage_publish = DockerOperator.partial(
        task_id="publish",
        execution_timeout=timedelta(minutes=10),
        **_docker_defaults,
    ).expand(command=publish_cmds)

    stage_train.set_upstream(stage_data)
    stage_validate.set_upstream(stage_train)
    stage_inference.set_upstream(stage_validate)
    stage_publish.set_upstream(stage_inference)
