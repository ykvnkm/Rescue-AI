# Infrastructure

Каталог инфраструктурных артефактов Rescue-AI:

- `airflow/dags/` — DAG-файлы Apache Airflow (`rescue_batch_pipeline`).
- `postgres/init/` — SQL-скрипты инициализации Postgres (схема приложения и БД метаданных Airflow).
- `k8s/charts/` — Helm-чарты (зонтичный `rescue-ai` и пять прикладных подчартов).
- `k8s/values/` — values-файлы для двух профилей: `offline.yaml` (наземная станция) и `cloud.yaml` (центральный кластер).
- `k8s/vault/` — политики Vault per-service.
- `observability/` — конфигурации Prometheus, Alertmanager и Grafana.
- `offline/` — отдельный docker-compose offline-стека (исторический).
- `docker-compose.platform.yml` — **архивный** файл, см. ниже.

## Apache Airflow

С момента рефакторинга P1 Airflow развёртывается из корневого `docker-compose.yml` репозитория. Три контейнера (`airflow-init`, `airflow-webserver`, `airflow-scheduler`) поднимаются вместе с прикладным стеком одной командой `docker compose up -d` из корня. Метаданные хранятся в БД `airflow` того же Postgres-инстанса.

Файл `infra/docker-compose.platform.yml` оставлен как архивный — он реализовывал ту же задачу до объединения, отдельным платформенным стеком с собственной БД. Использовать его не нужно; если когда-либо понадобится разделить жизненные циклы Airflow и приложения (например, держать платформу одной командой на отдельном сервере), его можно достать из git-истории и адаптировать.

## DAG `rescue_batch_pipeline`

Файл: `infra/airflow/dags/rescue_batch_daily.py`. Запускается ежедневно (`@daily`) с `catchup=True`. Три задачи идут последовательно через `DockerOperator`:

```
prepare_dataset → evaluate_model → publish_metrics
```

Каждая стадия выполняется в отдельном контейнере `rescue-ai:dev` командой `python -m rescue_ai.interfaces.cli.batch --stage <stage>`. Передача данных между стадиями выполняется через S3 (JSON-файлы). Финальная стадия идемпотентно делает upsert строки в таблицу `batch_pipeline_metrics`.

Канонический контракт stage-runner (`rescue_ai/interfaces/cli/batch.py`):

- вход: `--stage`, дата через `--ds` или `BATCH_TARGET_DATE`, опциональный allow-list через `--mission-ids-csv` или `BATCH_MISSION_IDS_CSV`;
- выход: `status` и `output_uri` (JSON в stdout);
- пустой день: процесс завершается с exit code `42`, и Airflow помечает таск как `skipped` (см. параметр `skip_exit_code` в `_COMMON` DAG-а).

Connections настраиваются через переменные окружения `AIRFLOW_CONN_RESCUE_APP_DB` и `AIRFLOW_CONN_RESCUE_S3` (Airflow автоматически регистрирует их при старте). В DAG-коде обращение через `BaseHook.get_connection(...)`.

## Запуск backfill (стандартный сценарий)

```bash
# 1. Поднять стек из корня
docker compose up -d

# 2. Подождать, пока airflow-webserver станет healthy
docker compose logs -f airflow-webserver
# Ctrl+C когда увидишь "Listening at: http://0.0.0.0:8080"

# 3. UI: http://localhost:8080 (логин/пароль из локального env)
#    Найди DAG rescue_batch_pipeline, включи переключатель, нажми ▷

# 4. Или CLI — backfill за диапазон дат:
docker compose exec airflow-scheduler \
    airflow dags backfill rescue_batch_pipeline \
    --start-date 2026-03-10 --end-date 2026-03-12
```

## Хранение метрик

После прогона DAG таблица `batch_pipeline_metrics` содержит по одной строке на пару `(ds, mission_id)`. Состав колонок описан в разделе 3.5.4 диплома. Микросервис `rescue-ai-batch-exporter` раз в `BATCH_EXPORTER_SCRAPE_INTERVAL_SEC` секунд читает последнюю запись и проектирует значения в Prometheus-gauge'и; Grafana-дашборд «Качество ML-модели» затем визуализирует эти значения.

## S3-layout артефактов batch-DAG

```
<ARTIFACTS_S3_PREFIX>/batch/ml_pipeline/
    ds=<ds>/
        mission=<mission_id>/
            dataset.json
            evaluation_<model_version>_<config_version>.json
```

## Runbook операций batch-сервиса

См. `docs/runbooks/batch_operations.md`.
