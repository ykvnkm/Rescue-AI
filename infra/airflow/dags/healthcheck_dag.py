from datetime import datetime

from airflow import DAG
from airflow.operators.python import PythonOperator


def platform_healthcheck() -> None:
    print("rescue-platform airflow DAG is running")


with DAG(
    dag_id="platform_healthcheck",
    start_date=datetime(2025, 1, 1),
    schedule="@hourly",
    catchup=False,
    tags=["platform", "bootstrap"],
) as dag:
    PythonOperator(
        task_id="print_healthcheck",
        python_callable=platform_healthcheck,
    )
