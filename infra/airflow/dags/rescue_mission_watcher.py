"""Rescue-AI mission watcher DAG.

Polls S3 every 5 minutes for new missions.
When a new mission is detected, triggers rescue_batch_pipeline
passing the full list (old + new) of mission IDs.

S3 layout: missions/{mission_uuid}/frames/*.jpg
Known missions persisted in Airflow Variable ``rescue_known_missions``.
ShortCircuitOperator skips trigger entirely when nothing is new.
"""

from __future__ import annotations

import json
import os
from datetime import datetime as std_datetime
from typing import Any, Literal

import boto3

try:
    from airflow import DAG as DagClass
    from airflow.models import Variable as VariableClass
    from airflow.operators.python import (
        ShortCircuitOperator as ShortCircuitOperatorClass,
    )
    from airflow.operators.trigger_dagrun import (
        TriggerDagRunOperator as TriggerDagRunOperatorClass,
    )
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

    class _FallbackVariable:
        @staticmethod
        def get(_key: str, default_var: str = "") -> str:
            return default_var

        @staticmethod
        def set(_key: str, _value: str) -> None:
            return None

    class _FallbackShortCircuitOperator:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._args = args
            self._kwargs = kwargs

    class _FallbackTriggerDagRunOperator:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._args = args
            self._kwargs = kwargs

        def set_upstream(self, _other: object) -> None:
            return None

    DagClass = _FallbackDAG
    VariableClass = _FallbackVariable
    ShortCircuitOperatorClass = _FallbackShortCircuitOperator
    TriggerDagRunOperatorClass = _FallbackTriggerDagRunOperator
    DateTimeFactory = std_datetime

DAG = DagClass
Variable = VariableClass
ShortCircuitOperator = ShortCircuitOperatorClass
TriggerDagRunOperator = TriggerDagRunOperatorClass
datetime = DateTimeFactory


PIPELINE_DAG_ID = "rescue_batch_pipeline"
VARIABLE_KEY = "rescue_known_missions"
# missions that are output artifacts, not input missions
_EXCLUDE = {"batch"}


def _list_missions_from_s3() -> set[str]:
    """List mission IDs using S3 CommonPrefixes (fast, no full scan)."""
    client = boto3.client(
        "s3",
        endpoint_url=os.environ.get("ARTIFACTS_S3_ENDPOINT") or None,
        region_name=os.environ.get("ARTIFACTS_S3_REGION") or None,
        aws_access_key_id=os.environ.get("ARTIFACTS_S3_ACCESS_KEY_ID") or None,
        aws_secret_access_key=os.environ.get("ARTIFACTS_S3_SECRET_ACCESS_KEY") or None,
    )
    bucket = os.environ.get("ARTIFACTS_S3_BUCKET", "")
    prefix = os.environ.get("ARTIFACTS_S3_PREFIX", "missions").strip("/")
    search_prefix = f"{prefix}/" if prefix else ""

    paginator = client.get_paginator("list_objects_v2")
    found: set[str] = set()
    for page in paginator.paginate(Bucket=bucket, Prefix=search_prefix, Delimiter="/"):
        for common in page.get("CommonPrefixes", []):
            segment = common["Prefix"].rstrip("/").split("/")[-1]
            if segment and segment not in _EXCLUDE:
                found.add(segment)
    return found


def _check_and_save(**context) -> bool:
    """Detect new missions; push list to XCom; return False to short-circuit if none."""
    known: set[str] = set(json.loads(Variable.get(VARIABLE_KEY, default_var="[]")))
    current = _list_missions_from_s3()
    new_missions = current - known

    if not new_missions:
        print(f"No new missions. Known: {sorted(known)}")
        return False

    all_missions = sorted(current)
    print(f"New missions: {sorted(new_missions)}")
    print(f"All missions: {all_missions}")
    Variable.set(VARIABLE_KEY, json.dumps(all_missions))
    context["ti"].xcom_push(key="all_missions", value=json.dumps(all_missions))
    return True


with DAG(
    dag_id="rescue_mission_watcher",
    description="Polls S3 every 5 min; triggers pipeline only when new mission appears",
    start_date=datetime(2026, 3, 1),
    schedule="*/5 * * * *",
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 0},
    tags=["rescue-ai", "watcher"],
) as dag:

    check = ShortCircuitOperator(
        task_id="detect_new_missions",
        python_callable=_check_and_save,
    )

    trigger = TriggerDagRunOperator(
        task_id="trigger_pipeline",
        trigger_dag_id=PIPELINE_DAG_ID,
        conf={
            "mission_ids": (
                "{{ ti.xcom_pull(task_ids='detect_new_missions', key='all_missions') }}"
            )
        },
        wait_for_completion=False,
    )

    trigger.set_upstream(check)
