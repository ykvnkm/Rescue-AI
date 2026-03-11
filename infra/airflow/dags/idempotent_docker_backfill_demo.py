from __future__ import annotations

from pathlib import Path

from airflow import DAG
from airflow.operators.python import ShortCircuitOperator
from airflow.providers.docker.operators.docker import DockerOperator
from docker.types import Mount
from pendulum import datetime

DAG_ID = "idempotent_docker_backfill_demo"
MARKERS_DIR = Path("/opt/airflow/data/markers")


def should_process(run_date: str) -> bool:
    """Skip processing when marker for this logical date already exists."""
    marker = MARKERS_DIR / f"{run_date}.done"
    return not marker.exists()


with DAG(
    dag_id=DAG_ID,
    description="Daily idempotent batch with DockerOperator + backfill support",
    start_date=datetime(2026, 3, 1),
    schedule="@daily",
    catchup=True,
    max_active_runs=1,
    tags=["platform", "docker", "idempotent", "backfill"],
) as dag:
    guard_idempotency = ShortCircuitOperator(
        task_id="guard_idempotency",
        python_callable=should_process,
        op_kwargs={"run_date": "{{ ds }}"},
    )

    process_day = DockerOperator(
        task_id="process_day",
        image="python:3.11-slim",
        docker_url="unix://var/run/docker.sock",
        api_version="auto",
        auto_remove="success",
        mount_tmp_dir=False,
        mounts=[Mount(source="airflow_shared_data", target="/data", type="volume")],
        environment={"RUN_DATE": "{{ ds }}"},
        command=(
            "python -c \""
            "from pathlib import Path; "
            "import os; "
            "run_date=os.environ['RUN_DATE']; "
            "output=Path('/data/results') / f'{run_date}.json'; "
            "marker=Path('/data/markers') / f'{run_date}.done'; "
            "output.parent.mkdir(parents=True, exist_ok=True); "
            "marker.parent.mkdir(parents=True, exist_ok=True); "
            "output.write_text('{\\\"run_date\\\":\\\"'+run_date+'\\\",\\\"status\\\":\\\"processed\\\"}\\n', encoding='utf-8'); "
            "marker.write_text('ok\\n', encoding='utf-8'); "
            "print(f'processed {run_date}')"
            "\""
        ),
    )

    guard_idempotency >> process_day
