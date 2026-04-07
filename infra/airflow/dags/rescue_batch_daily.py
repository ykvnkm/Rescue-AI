"""Rescue-AI daily batch ML pipeline.

One DAG, runs once per day, processes every mission that has frames in
S3 through the four stages of the pipeline::

    data → train → validate → publish

Design notes for defence
------------------------
* **Simple graph** — exactly 4 DockerOperator tasks in UI:
  ``data -> train -> validate -> publish``.
  No dynamic mapping and no helper ``build_*`` tasks.

* **Idempotency — two layers, different jobs:**

    1. *Per-ds artifact skip (S3).* Every stage writes a deterministic
       JSON key under ``ml_pipeline/mission={id}/ds={ds}/...`` and checks
       ``store.exists(...)`` before doing any work. Retry of a failed
       task inside the same ds skips already-completed stages. S3 grows
       ``O(N_missions × N_days)`` — bounded by a bucket lifecycle policy
       (Glacier after 30d, expire after 180d), not by the pipeline.

    2. *Cross-day mission-level upsert (Postgres).* The ``publish``
       stage upserts into ``batch_pipeline_metrics`` keyed on
       ``(mission_id, model_version, code_version)``. Exactly ONE row
       per mission × model × code-version for the whole lifetime of
       the system; every successful run overwrites that row and bumps
       ``ds`` + ``updated_at``. The table grows
       ``O(N_missions × N_model_versions × N_code_versions)`` —
       independent of how many days the pipeline has been running.

* **Mission discovery is cached.** Only the ``data`` stage calls
  ``list_objects_v2``; it writes the resolved mission set to a small
  JSON manifest at ``.../ml_pipeline/ds={ds}/missions.json``. Downstream
  stages read that manifest with a single ``get_object``, so the DAG
  makes ~1 LIST per day instead of 4, and the mission set is pinned for
  the whole run (no races if a new mission is uploaded mid-run).

* **``catchup=False`` is intentional.** The pipeline input is a set of
  static mission folders in S3, *not* a time-partitioned event stream.
  A missed day has no unique data: the next day's run re-processes the
  same mission set and upserts the same row. ``ds`` is a provenance tag
  ("when was this row last refreshed"), not an event partition key.
  If time-series drift tracking is ever needed, the correct fix is a
  separate ``batch_pipeline_metrics_history`` append-only table — NOT
  ``catchup=True`` (which would just waste compute re-running identical
  inputs).

* **Secrets** come from Airflow Connections registered via the standard
  ``AIRFLOW_CONN_*`` environment variable mechanism, populated from
  GitHub Secrets in CI. Nothing is hardcoded; ``BaseHook.get_connection``
  is the single source of truth.

* **conf parameters** (``dag_run.conf``):
    - ``mission_ids_csv`` — optional comma-separated mission IDs to
      restrict processing to a subset (filters the manifest, does not
      bypass it);
    - ``model_version`` / ``code_version`` — labels written into
      artifact keys and PG rows.

S3 layout (input): ``missions/{mission_uuid}/frames/*.jpg``
S3 layout (output): ``missions/batch/ml_pipeline/mission=<uuid>/ds=<date>/*.json``
"""

from __future__ import annotations

import os
from datetime import datetime as std_datetime
from datetime import timedelta
from typing import Any, Literal, cast

_AirflowDAGClass: Any
_BaseHookClass: Any
_ParamClass: Any
_DockerOperatorClass: Any
_MountClass: Any
_DateTimeFactory: Any

try:
    from airflow import DAG as AirflowDAGClass
    from airflow.hooks.base import BaseHook as AirflowBaseHookClass
    from airflow.models.param import Param as AirflowParamClass
    from airflow.providers.docker.operators.docker import (
        DockerOperator as AirflowDockerOperatorClass,
    )
    from docker.types import Mount as DockerMountClass
    from pendulum import datetime as PendulumDateTimeFactory

    _AirflowDAGClass = AirflowDAGClass
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
    _BaseHookClass = cast(Any, _FallbackBaseHook)
    _ParamClass = cast(Any, _FallbackParam)
    _DockerOperatorClass = cast(Any, _FallbackDockerOperator)
    _MountClass = cast(Any, _FallbackMount)
    _DateTimeFactory = cast(Any, std_datetime)

DAG = _AirflowDAGClass
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


def _build_stage_command(stage: str) -> list[str]:
    """Return templated argv for one pipeline stage."""
    return [
        "python",
        "-m",
        "rescue_ai.interfaces.cli.batch",
        "--stage",
        stage,
        "--all-missions",
        "--mission-ids-csv",
        "{{ params.mission_ids_csv if params.mission_ids_csv is not none else '' }}",
        "--ds",
        "{{ ds }}",
        "--model-version",
        "{{ params.model_version }}",
        "--code-version",
        "{{ params.code_version }}",
    ]


# ── DAG definition ──────────────────────────────────────────────

with DAG(
    dag_id=DAG_ID,
    description="Rescue-AI daily batch: data → train → validate → publish",
    start_date=datetime(2026, 3, 1),
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
    # -- Shared DockerOperator defaults ------------------------------

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

    # -- Five fixed stage tasks --------------------------------------

    stage_data = DockerOperator(
        task_id="data",
        execution_timeout=timedelta(minutes=30),
        command=_build_stage_command("data"),
        **_docker_defaults,
    )

    stage_train = DockerOperator(
        task_id="train",
        execution_timeout=timedelta(hours=2),
        command=_build_stage_command("train"),
        **_docker_defaults,
    )

    stage_validate = DockerOperator(
        task_id="validate",
        execution_timeout=timedelta(hours=1),
        command=_build_stage_command("validate"),
        **_docker_defaults,
    )

    stage_publish = DockerOperator(
        task_id="publish",
        execution_timeout=timedelta(minutes=10),
        command=_build_stage_command("publish"),
        **_docker_defaults,
    )

    stage_train.set_upstream(stage_data)
    stage_validate.set_upstream(stage_train)
    stage_publish.set_upstream(stage_validate)
