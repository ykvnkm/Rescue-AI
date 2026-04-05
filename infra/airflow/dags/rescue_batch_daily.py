"""Rescue-AI batch ML pipeline DAG.

Triggered automatically by rescue_mission_watcher when a new mission
appears in S3.  Runs four sequential stages across ALL known missions:

    data → train → validate → inference

A @task resolves the mission list from dag_run.conf (set by watcher)
or falls back to the Airflow Variable so manual triggers also work.
This avoids any Jinja quoting issues with the env dict.

S3 layout: missions/{mission_uuid}/frames/*.jpg
"""

from __future__ import annotations

import json
import os
from datetime import datetime as std_datetime
from datetime import timedelta
from typing import Any, Literal

try:
    from airflow import DAG as DagClass
    from airflow.decorators import task as TaskDecorator
    from airflow.models import Variable as VariableClass
    from airflow.providers.docker.operators.docker import (
        DockerOperator as DockerOperatorClass,
    )
    from docker.types import Mount as MountClass
    from pendulum import datetime as DateTimeFactory
except ImportError:

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

    class _FallbackVariable:
        @staticmethod
        def get(_key: str, default_var: str = "") -> str:
            return default_var

    class _FallbackMount:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._args = args
            self._kwargs = kwargs

    class _FallbackDockerOperator:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._args = args
            self._kwargs = kwargs

        def set_upstream(self, _other: object) -> None:
            return None

    DagClass = _FallbackDAG
    TaskDecorator = _fallback_task
    VariableClass = _FallbackVariable
    DockerOperatorClass = _FallbackDockerOperator
    MountClass = _FallbackMount
    DateTimeFactory = std_datetime

DAG = DagClass
task = TaskDecorator
Variable = VariableClass
DockerOperator = DockerOperatorClass
Mount = MountClass
datetime = DateTimeFactory


DAG_ID = "rescue_batch_pipeline"
MODEL_VERSION = "yolov8n_baseline_multiscale"
CODE_VERSION = "main"
BATCH_IMAGE = os.environ.get("BATCH_IMAGE", "rescue-ai-batch:local")
VARIABLE_KEY = "rescue_known_missions"

_BASE_ENV = {
    "DB_DSN": os.environ.get("DB_DSN", ""),
    "ARTIFACTS_S3_ENDPOINT": os.environ.get("ARTIFACTS_S3_ENDPOINT", ""),
    "ARTIFACTS_S3_BUCKET": os.environ.get("ARTIFACTS_S3_BUCKET", ""),
    "ARTIFACTS_S3_PREFIX": os.environ.get("ARTIFACTS_S3_PREFIX", ""),
    "ARTIFACTS_S3_ACCESS_KEY_ID": os.environ.get("ARTIFACTS_S3_ACCESS_KEY_ID", ""),
    "ARTIFACTS_S3_SECRET_ACCESS_KEY": os.environ.get(
        "ARTIFACTS_S3_SECRET_ACCESS_KEY", ""
    ),
    "ARTIFACTS_S3_REGION": os.environ.get("ARTIFACTS_S3_REGION", ""),
    # Resolved by resolve_missions @task and injected cleanly via XCom
    "MISSION_IDS": "{{ ti.xcom_pull(task_ids='resolve_missions') }}",
    "RUN_DS": "{{ ds }}",
}

_DOCKER_DEFAULTS = {
    "image": BATCH_IMAGE,
    "docker_url": "unix://var/run/docker.sock",
    "api_version": "auto",
    "force_pull": False,
    "auto_remove": "success",
    "mount_tmp_dir": False,
    "environment": _BASE_ENV,
    "mounts": [
        Mount(source="airflow_shared_data", target="/opt/airflow/data", type="volume")
    ],
}


def _stage_cmd(stage: str) -> str:
    """Python one-liner inside container: parse MISSION_IDS env, run CLI per mission."""
    return (
        'python3 -c "'
        "import json,os,subprocess,sys; "
        f"st='{stage}'; mv='{MODEL_VERSION}'; cv='{CODE_VERSION}'; "
        "ids=json.loads(os.environ['MISSION_IDS']); "
        "ds=os.environ['RUN_DS']; "
        "errs=[m for m in ids if subprocess.run(["
        "'python3','-m','rescue_ai.interfaces.cli.batch',"
        "'--stage',st,'--mission-id',m,'--ds',ds,"
        "'--model-version',mv,'--code-version',cv"
        "],check=False).returncode!=0]; "
        'sys.exit(1) if errs else None"'
    )


with DAG(
    dag_id=DAG_ID,
    description="Rescue-AI: data → train → validate → inference (all missions)",
    start_date=datetime(2026, 3, 1),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    default_args={
        "retries": 1,
        "retry_delay": timedelta(minutes=15),
    },
    tags=["rescue-ai", "ml-pipeline", "batch"],
) as dag:

    @task
    def resolve_missions(**context) -> str:
        """Return JSON list of mission IDs from conf or Variable fallback."""
        dag_run = context.get("dag_run")
        conf_val = dag_run.conf.get("mission_ids") if dag_run else None
        if conf_val:
            # conf_val is already a JSON string like '["uuid1","uuid2"]'
            missions = json.loads(conf_val) if isinstance(conf_val, str) else conf_val
            return json.dumps(sorted(missions))
        # Fallback: use last known list from Variable
        raw = Variable.get(VARIABLE_KEY, default_var="[]")
        missions = json.loads(raw)
        if not missions:
            raise ValueError(
                f"No mission_ids in conf and Variable '{VARIABLE_KEY}' is empty. "
                'Trigger manually with: {"mission_ids": "[\\"<uuid>\\"]"}  '
                "or let the watcher detect missions from S3."
            )
        return json.dumps(sorted(missions))

    mission_ids = resolve_missions()

    stage_data = DockerOperator(
        task_id="data",
        command=_stage_cmd("data"),
        execution_timeout=timedelta(minutes=30),
        **_DOCKER_DEFAULTS,
    )

    stage_train = DockerOperator(
        task_id="train",
        command=_stage_cmd("train"),
        execution_timeout=timedelta(hours=2),
        **_DOCKER_DEFAULTS,
    )

    stage_validate = DockerOperator(
        task_id="validate",
        command=_stage_cmd("validate"),
        execution_timeout=timedelta(hours=1),
        **_DOCKER_DEFAULTS,
    )

    stage_inference = DockerOperator(
        task_id="inference",
        command=_stage_cmd("inference"),
        execution_timeout=timedelta(hours=6),
        sla=timedelta(hours=8),
        **_DOCKER_DEFAULTS,
    )

    stage_data.set_upstream(mission_ids)
    stage_train.set_upstream(stage_data)
    stage_validate.set_upstream(stage_train)
    stage_inference.set_upstream(stage_validate)
