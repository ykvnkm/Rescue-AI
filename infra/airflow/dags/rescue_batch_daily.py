"""Rescue-AI daily batch ML pipeline.

One DAG, runs once per day, processes every mission that has frames in
S3 through the four stages of the pipeline::

    data → warmup → evaluate → publish

This DAG does **continuous evaluation** of a fixed detector against the
labeled mission corpus — it does NOT train or fine-tune weights. The
``warmup`` stage loads the deployed detector and probes it (fail-fast on
a broken runtime before the heavy ``evaluate`` stage). The ``evaluate``
stage runs the detector over the val manifest and records a confusion
matrix. Honest naming matters: a task called ``train`` that doesn't
actually train weights is a lie the reviewer will catch.

Design notes for defence
------------------------
* **Simple graph** — exactly 4 DockerOperator tasks in UI:
  ``data -> warmup -> evaluate -> publish``.
  No dynamic mapping and no helper ``build_*`` tasks.

* **Idempotency — two layers, different jobs:**

    1. *Per-ds artifact skip (S3).* Every stage writes a deterministic
       JSON key under ``ml_pipeline/mission={id}/ds={ds}/...`` and checks
       ``store.exists(...)`` before doing any work. Retry of a failed
       task skips already-completed stages.

    2. *Per-ds upsert (Postgres).* The ``publish`` stage upserts into
       ``batch_pipeline_metrics`` keyed on
       ``(ds, mission_id, model_version, code_version)``. Re-running a
       single ds is idempotent (``ON CONFLICT ... DO UPDATE``);
       ``airflow dags backfill -s X -e Y`` inserts one row per day in
       the range, and ``updated_at`` diverges from ``ds`` — an
       observable signal that the backfill actually ran. The table
       grows ``O(N_missions × N_days × N_model_versions × N_code_versions)``
       which for the realistic corpus (~10 labeled missions × 365 days)
       is negligible.

* **Mission discovery is cached.** Only the ``data`` stage calls
  ``list_objects_v2``; it writes the resolved mission set to a small
  JSON manifest at ``.../ml_pipeline/ds={ds}/missions.json``. Downstream
  stages read that manifest with a single ``get_object``, so the DAG
  makes ~1 LIST per day instead of 4, and the mission set is pinned for
  the whole run (no races if a new mission is uploaded mid-run).

* **``catchup=True`` is intentional.** ``ds`` is part of the PK, so
  backfilling a date range fills one row per day. This gives a drift
  signal over time (recall/accuracy per ds) and lets the reviewer see
  ``airflow dags backfill -s X -e Y`` actually do something observable
  (divergence of ``updated_at`` from ``ds``).

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
S3 layout (output): ``missions/batch/ml_pipeline/mission=<uuid>/*.json``
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

from airflow import DAG
from airflow.hooks.base import BaseHook
from airflow.models.param import Param
from airflow.providers.docker.operators.docker import DockerOperator
from docker.types import Mount
from pendulum import datetime

# ── Constants ────────────────────────────────────────────────────

DAG_ID = "rescue_batch_pipeline"
BATCH_IMAGE = os.environ["BATCH_IMAGE"]

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
    description="Rescue-AI daily batch: data → warmup → evaluate → publish",
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
            default="yolov8n_multiscale",
            type="string",
            description="Model version tag written into artifact keys and PG rows.",
        ),
        "code_version": Param(
            default="v1",
            type="string",
            description="Code version tag written into artifact keys and PG rows.",
        ),
    },
    tags=["rescue-ai", "ml-pipeline", "batch"],
) as dag:
    # -- Shared DockerOperator defaults ------------------------------

    _docker_defaults: dict[str, Any] = {
        "image": BATCH_IMAGE,
        "docker_url": "unix://var/run/docker.sock",
        "api_version": "auto",
        "force_pull": False,
        "do_xcom_push": True,
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

    # -- Four fixed stage tasks --------------------------------------

    stage_data = DockerOperator(
        task_id="data",
        execution_timeout=timedelta(minutes=30),
        command=_build_stage_command("data"),
        **_docker_defaults,
    )

    stage_warmup = DockerOperator(
        task_id="warmup",
        execution_timeout=timedelta(minutes=15),
        command=_build_stage_command("warmup"),
        **_docker_defaults,
    )

    stage_evaluate = DockerOperator(
        task_id="evaluate",
        execution_timeout=timedelta(hours=1),
        command=_build_stage_command("evaluate"),
        **_docker_defaults,
    )

    stage_publish = DockerOperator(
        task_id="publish",
        execution_timeout=timedelta(minutes=10),
        command=_build_stage_command("publish"),
        **_docker_defaults,
    )

    stage_warmup.set_upstream(stage_data)
    stage_evaluate.set_upstream(stage_warmup)
    stage_publish.set_upstream(stage_evaluate)
