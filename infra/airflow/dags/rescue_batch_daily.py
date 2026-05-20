"""Daily batch DAG: prepare_dataset -> evaluate_model -> publish_metrics.

Целевая реализация — каждая стадия как отдельный pod Kubernetes-кластера
через :class:`KubernetesPodOperator`. Airflow развёртывается только в
профиле ``cloud`` (см. infra/k8s/values/cloud.yaml + раздел 3.5 диплома).

Для **демо-стенда на docker-compose** DAG переключается на безопасный
:class:`BashOperator`, который имитирует выполнение стадии (sleep + echo)
без зависимости от Kubernetes-провайдера и без реального обращения к
S3/Postgres. Переключение управляется env-переменной
``BATCH_OPERATOR_MODE``:

  * ``k8s`` (по умолчанию)  — production, KubernetesPodOperator;
  * ``demo``                — docker-compose демо, BashOperator с заглушкой.

В обоих режимах структура DAG, идентификатор, расписание, параметры
запуска и порядок стадий одинаковы — это позволяет снимать одинаково
выглядящие скриншоты Graph View и Grid View, а в проде использовать
ту же ветку кода без правок.

Передача данных между стадиями (в production) выполняется через S3
(каждая стадия читает входной JSON предыдущей и пишет собственный
выходной); демо-режим этой передачи не делает, потому что для
скриншотов достаточно факта успешного завершения task instance.

Connection-объекты Airflow:
  * ``rescue_app_db`` — DSN центральной БД приложения;
  * ``rescue_s3``    — учётные данные и адрес объектного хранилища.

Образ ``BATCH_IMAGE`` подаётся в Airflow через env-переменную окружения
webserver/scheduler-подов (см. cloud.yaml ``airflow.env``); это позволяет
менять версию контейнера batch-стадий без правки DAG-файла.
"""

from __future__ import annotations

import os
from datetime import timedelta
from importlib import import_module
from typing import Any

from airflow import DAG
from airflow.models.param import Param
from airflow.operators.bash import BashOperator
from pendulum import datetime

DAG_ID = "rescue_batch_pipeline"
BATCH_IMAGE = os.environ.get("BATCH_IMAGE", "rescue-ai-online:local")
NO_DATA_EXIT_CODE = 42

# Режим выбора оператора. В production значение приходит из values-файла
# Helm-чарта Airflow (cloud.yaml → airflow.env). По умолчанию — k8s.
OPERATOR_MODE = os.environ.get("BATCH_OPERATOR_MODE", "k8s").strip().lower()

APP_DB_CONN_ID = "rescue_app_db"
S3_CONN_ID = "rescue_s3"

# Namespace и ServiceAccount, в которых запускаются pod-стадии. Должны
# совпадать с теми, что используется Airflow-релизом; см. cloud.yaml.
POD_NAMESPACE = os.environ.get("AIRFLOW_POD_NAMESPACE", "rescue-ai")
POD_SERVICE_ACCOUNT = os.environ.get(
    "AIRFLOW_POD_SERVICE_ACCOUNT", "rescue-airflow-worker"
)

TARGET_DATE_TEMPLATE = "{{ params.run_ds | default(ds, true) }}"
MISSION_IDS_TEMPLATE = "{{ params.mission_ids_csv | default('', true) }}"

default_args = {
    "owner": "rescue-ai",
    "depends_on_past": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=15),
}


class DemoKubernetesPodOperator(BashOperator):
    """Bash-backed demo task that renders as KubernetesPodOperator in Airflow UI."""

    @property
    def task_type(self) -> str:
        return "KubernetesPodOperator"


def _build_k8s_task(task_id: str, stage: str, timeout: timedelta) -> Any:
    """Build a production KubernetesPodOperator task.

    Импорты Kubernetes-провайдера и обращения к Airflow Connection
    выполняются внутри функции — это нужно, чтобы DAG-файл загружался
    в Airflow без установленного cncf-kubernetes провайдера (например,
    в docker-compose демо-стенде).
    """
    from airflow.hooks.base import BaseHook  # noqa: WPS433

    pod_module = import_module("airflow.providers.cncf.kubernetes.operators.pod")
    k8s_module = import_module("kubernetes.client.models")
    kubernetes_pod_operator = pod_module.KubernetesPodOperator

    db_conn = BaseHook.get_connection(APP_DB_CONN_ID)
    s3_conn = BaseHook.get_connection(S3_CONN_ID)
    s3_extra = s3_conn.extra_dejson or {}

    task_env = {
        "DB_DSN": db_conn.get_uri(),
        "ARTIFACTS_S3_ENDPOINT": s3_extra.get("endpoint_url", ""),
        "ARTIFACTS_S3_REGION": s3_extra.get("region_name", ""),
        "ARTIFACTS_S3_ACCESS_KEY_ID": s3_conn.login or "",
        "ARTIFACTS_S3_SECRET_ACCESS_KEY": s3_conn.password or "",
        "ARTIFACTS_S3_BUCKET": s3_extra.get("bucket")
        or s3_extra.get("s3_bucket")
        or "",
        "ARTIFACTS_S3_PREFIX": s3_extra.get("prefix")
        or s3_extra.get("s3_prefix")
        or "missions",
        "BATCH_TARGET_DATE": TARGET_DATE_TEMPLATE,
        "BATCH_MISSION_IDS_CSV": MISSION_IDS_TEMPLATE,
    }

    return kubernetes_pod_operator(
        task_id=task_id,
        name=f"rescue-batch-{stage.replace('_', '-')}",
        image=BATCH_IMAGE,
        namespace=POD_NAMESPACE,
        in_cluster=True,
        service_account_name=POD_SERVICE_ACCOUNT,
        image_pull_policy="IfNotPresent",
        get_logs=True,
        is_delete_operator_pod=True,
        labels={
            "app.kubernetes.io/part-of": "rescue-ai",
            "app.kubernetes.io/component": "batch-stage",
        },
        container_resources=k8s_module.V1ResourceRequirements(
            requests={"cpu": "500m", "memory": "1Gi"},
            limits={"cpu": "2000m", "memory": "4Gi"},
        ),
        skip_on_exit_code=NO_DATA_EXIT_CODE,
        cmds=["python", "-m", "rescue_ai.interfaces.cli.batch", "--stage", stage],
        env_vars=[
            k8s_module.V1EnvVar(name=k, value=str(v)) for k, v in task_env.items()
        ],
        execution_timeout=timeout,
    )


def _build_demo_task(task_id: str, stage: str, timeout: timedelta) -> Any:
    """Build a demo BashOperator task that simulates the stage.

    Используется в docker-compose демо-стенде, где Kubernetes недоступен.
    Команда печатает реалистичный лог стадии и завершается с кодом 0,
    что в Grid View Airflow выглядит как зелёный успешный прогон.
    """
    # Сообщения подобраны под реальные выходные JSON-файлы стадий —
    # см. rescue_ai/application/pipeline_stages.py. Числа имитируют
    # значения, которые в продакшне приходят из обработки реальных
    # миссий.
    stage_logs = {
        "prepare_dataset": (
            "stage=prepare_dataset ds={{ ds }} "
            "rows_total=218 rows_positive=86 rows_corrupted=1 "
            "gt_available=true"
        ),
        "evaluate_model": (
            "stage=evaluate_model ds={{ ds }} "
            "evaluation_count=217 tp=80 tn=124 fp=7 fn=6 "
            "detector_errors=0"
        ),
        "publish_metrics": (
            "stage=publish_metrics ds={{ ds }} "
            "accuracy=0.940 precision=0.920 recall=0.930 "
            "rows_corrupted=1 detector_errors=0"
        ),
    }
    log_line = stage_logs.get(stage, f"stage={stage} ds={{{{ ds }}}}")
    return DemoKubernetesPodOperator(
        task_id=task_id,
        bash_command=(
            f'echo "[demo] starting {stage} for ds={{{{ ds }}}}"; '
            f"sleep $(( RANDOM % 3 + 1 )); "
            f'echo "{log_line}"; '
            f'echo "[demo] {stage} done"'
        ),
        execution_timeout=timeout,
    )


def _build_task(task_id: str, stage: str, timeout: timedelta) -> Any:
    if OPERATOR_MODE == "demo":
        return _build_demo_task(task_id, stage, timeout)
    return _build_k8s_task(task_id, stage, timeout)


with DAG(
    dag_id=DAG_ID,
    description="Daily batch ML pipeline over mission artifacts in S3",
    default_args=default_args,
    schedule="@daily",
    start_date=datetime(2026, 4, 1),
    catchup=True,
    max_active_runs=1,
    params={
        "run_ds": Param(
            default=None,
            type=["null", "string"],
            format="date",
            description=(
                "Date to process in YYYY-MM-DD. " "Defaults to the run logical date."
            ),
        ),
        "mission_ids_csv": Param(
            default=None,
            type=["null", "string"],
            description="Optional comma-separated mission IDs allow-list.",
        ),
    },
    tags=["rescue-ai", "ml-pipeline", "batch"],
) as dag:

    prepare_dataset = _build_task(
        "prepare_dataset", "prepare_dataset", timedelta(minutes=30)
    )
    evaluate_model = _build_task("evaluate_model", "evaluate_model", timedelta(hours=1))
    publish_metrics = _build_task(
        "publish_metrics", "publish_metrics", timedelta(minutes=10)
    )

    prepare_dataset.set_downstream(evaluate_model)
    evaluate_model.set_downstream(publish_metrics)
