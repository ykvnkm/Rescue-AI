"""Daily batch DAG: prepare_dataset -> evaluate_model -> publish_metrics.

Каждая стадия запускается в отдельном ephemeral pod-е Kubernetes-кластера
через :class:`KubernetesPodOperator`. Airflow развёртывается только в
профиле ``cloud`` (см. infra/k8s/values/rescue-batch-cloud.yaml + раздел
3.5 пояснительной записки).

Передача данных между стадиями выполняется через S3 (каждая стадия
читает входной JSON предыдущей и пишет собственный выходной).

Connection-объекты Airflow:
  * ``rescue_app_db`` — DSN центральной БД приложения;
  * ``rescue_s3``    — учётные данные и адрес объектного хранилища.

Образ ``BATCH_IMAGE`` подаётся в Airflow через env-переменную окружения
webserver/scheduler-подов (см. rescue-batch-cloud.yaml ``airflow.env``);
это позволяет менять версию контейнера batch-стадий без правки DAG-файла.

DAG-файл загружается Airflow scheduler-ом в любом режиме (включая
docker-compose dev-стенд). Однако фактический запуск таски требует
доступа к Kubernetes API: в docker-compose dev-стенде Airflow UI
работает (Graph View, Code View), но trigger таски вернёт ошибку
подключения к kube-apiserver. Это сознательное решение: batch
запускается только в production-кластере, никакого fallback'а на
Bash-заглушку или DockerOperator-ветку в коде DAG-а нет.
"""

from __future__ import annotations

import os
from datetime import timedelta
from importlib import import_module
from typing import Any

from airflow import DAG
from airflow.models.param import Param
from pendulum import datetime

DAG_ID = "rescue_batch_pipeline"
BATCH_IMAGE = os.environ.get("BATCH_IMAGE", "rescue-ai-batch-worker:local")
NO_DATA_EXIT_CODE = 42

APP_DB_CONN_ID = "rescue_app_db"
S3_CONN_ID = "rescue_s3"

# Namespace и ServiceAccount, в которых запускаются pod-стадии.
# Дефолт = `rescue-batch` — отдельный namespace batch-контура (ADR-0008
# §4). Реальные значения задаются helm release rescue-batch через
# env-переменные пода Airflow (см. infra/k8s/values/rescue-batch-cloud.yaml).
POD_NAMESPACE = os.environ.get("AIRFLOW_POD_NAMESPACE", "rescue-batch")
POD_SERVICE_ACCOUNT = os.environ.get(
    "AIRFLOW_POD_SERVICE_ACCOUNT", "rescue-batch-worker"
)

TARGET_DATE_TEMPLATE = "{{ params.run_ds | default(ds, true) }}"
MISSION_IDS_TEMPLATE = "{{ params.mission_ids_csv | default('', true) }}"

default_args = {
    "owner": "rescue-ai",
    "depends_on_past": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=15),
}


def _build_k8s_task(task_id: str, stage: str, timeout: timedelta) -> Any:
    """Build a KubernetesPodOperator task for one stage.

    Импорты Kubernetes-провайдера и обращения к Airflow Connection
    выполняются внутри функции, чтобы DAG-файл загружался Airflow
    scheduler-ом даже в среде без cncf-kubernetes провайдера (например
    при первом импорте до установки extras).
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
                "Date to process in YYYY-MM-DD. Defaults to the run logical date."
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

    prepare_dataset = _build_k8s_task(
        "prepare_dataset", "prepare_dataset", timedelta(minutes=30)
    )
    evaluate_model = _build_k8s_task(
        "evaluate_model", "evaluate_model", timedelta(hours=1)
    )
    publish_metrics = _build_k8s_task(
        "publish_metrics", "publish_metrics", timedelta(minutes=10)
    )

    prepare_dataset.set_downstream(evaluate_model)
    evaluate_model.set_downstream(publish_metrics)
