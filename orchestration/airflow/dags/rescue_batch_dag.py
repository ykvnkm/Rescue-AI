from __future__ import annotations

from datetime import datetime

from airflow import DAG
from airflow.operators.python import PythonOperator


def _prepare_context(**context: object) -> None:
    _ = context


def _run_batch_service(**context: object) -> None:
    # Here we will call batch service CLI/container with idempotency key.
    _ = context


def _publish_artifacts(**context: object) -> None:
    # Here we will move generated artifacts to remote S3 storage.
    _ = context


def _notify_result(**context: object) -> None:
    # Here we will integrate Telegram/email notification adapters.
    _ = context


with DAG(
    dag_id="rescue_batch_pipeline",
    start_date=datetime(2025, 1, 1),
    schedule_interval=None,
    catchup=False,
    tags=["rescue-ai", "batch", "mlops"],
) as dag:
    prepare_context = PythonOperator(
        task_id="prepare_context",
        python_callable=_prepare_context,
    )
    run_batch = PythonOperator(
        task_id="run_batch_service",
        python_callable=_run_batch_service,
        retries=1,
    )
    publish_artifacts = PythonOperator(
        task_id="publish_artifacts",
        python_callable=_publish_artifacts,
    )
    notify_result = PythonOperator(
        task_id="notify_result",
        python_callable=_notify_result,
        trigger_rule="all_done",
    )

    prepare_context >> run_batch >> publish_artifacts >> notify_result

