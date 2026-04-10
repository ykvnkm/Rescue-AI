# Platform Skeleton

Инфраструктурный каркас для Airflow batch-контура:

- `Airflow` (`webserver`, `scheduler`, `init`)
- Docker-based execution of `rescue-ai-batch` image

## Быстрый старт

```bash
cd infra
cp platform.env.example platform.env
docker compose -f docker-compose.platform.yml --env-file platform.env up -d
```

UI/Endpoints:

- Airflow: `http://localhost:8080`

## Остановка

```bash
docker compose -f docker-compose.platform.yml --env-file platform.env down
```

## Важно

- В `platform.env.example` нет дефолтных секретов. Перед запуском заполните DSN и S3 ключи.
- `infra/postgres/init/010-app-schema.sql` — единый SQL со схемой продуктовых таблиц (`missions`, `alerts`, `frame_events`, `episodes`, `batch_pipeline_metrics`) в schema `app`.
- Airflow metadata хранится в schema `airflow` (через `AIRFLOW__DATABASE__SQL_ALCHEMY_SCHEMA=airflow`).
- Основной DAG batch-контура: `infra/airflow/dags/rescue_batch_daily.py`.

## DAG: Rescue Batch (DockerOperator + Idempotency + Backfill)

Подробный пошаговый runbook: `infra/AIRFLOW_ML_PIPELINE_RUNBOOK.md`.

`rescue_batch_pipeline`:

- Запускается ежедневно (`@daily`) с `catchup=True`.
- Три таски `DockerOperator`, идут последовательно:
  `prepare_dataset -> evaluate_model -> publish_metrics`.
- Каждая стадия на старте делает свежий `list_objects_v2` по
  `{prefix}/{ds}/` и итерируется по всем обнаруженным миссиям.
- Передача между тасками идёт через S3:
  `prepare_dataset` пишет `dataset.json`, `evaluate_model` читает его и
  пишет `evaluation_<mv>_<cv>.json`, `publish_metrics` апсертит сводную
  строку в `batch_pipeline_metrics`.
- Rerun семантика: `put_object` всегда перезаписывает артефакты,
  `publish_metrics` делает `ON CONFLICT DO UPDATE`. Никакого
  skip-by-exists и `--force` нет.
- Backfill — через `airflow dags backfill`.

Канонический контракт stage-runner (`rescue_ai/interfaces/cli/batch.py`):

- Вход: `--stage`, дата через `--ds` или `BATCH_TARGET_DATE`,
  опциональный allow-list через `--mission-ids-csv` или `BATCH_MISSION_IDS_CSV`.
- Выход: `status`, `output_uri` (JSON в stdout).
- Пустой день: процесс завершаетcя с exit code `42`, а DAG таск помечается как `skipped`.

## Пошаговый запуск Airflow и что смотреть

1. Подготовьте `infra/platform.env`:
```bash
cp infra/platform.env.example infra/platform.env
```
Заполните минимум:
`AIRFLOW_ADMIN_USER`, `AIRFLOW_ADMIN_PASSWORD`, `AIRFLOW_ADMIN_EMAIL`, `AIRFLOW_CONN_RESCUE_APP_DB`, `AIRFLOW_CONN_RESCUE_S3`. Секреты прокидываются в Airflow штатно — через env-переменные с префиксом `AIRFLOW_CONN_*`, которые Airflow автоматически регистрирует как Connections на старте. В DAG-е обращение через `BaseHook.get_connection("rescue_app_db" | "rescue_s3")`.

2. Проверьте конфиг compose:
```bash
docker compose -f infra/docker-compose.platform.yml --env-file infra/platform.env config -q
```

3. Поднимите платформу:
```bash
docker compose -f infra/docker-compose.platform.yml --env-file infra/platform.env up -d
```

4. Откройте Airflow UI (`http://localhost:8080`), включите DAG `rescue_batch_pipeline`, зайдите в Graph/Grid.

5. Запустите backfill за диапазон:
```bash
docker compose -f infra/docker-compose.platform.yml --env-file infra/platform.env exec airflow-webserver \
  airflow dags backfill rescue_batch_pipeline -s 2026-03-10 -e 2026-03-12
```

6. Проверьте артефакты в S3 (по префиксу):
`<ARTIFACTS_S3_PREFIX>/batch/ml_pipeline/ds=<ds>/mission=<mission_id>/`.

7. Проверьте сводные строки в Postgres по ключу
`(ds, mission_id)` в таблице `batch_pipeline_metrics`.

## Runbook (failed/partial)

- `failed` + `reason=empty_input`: проверить S3 префикс `<ARTIFACTS_S3_PREFIX>/<mission_id>/<ds>/images`.
- `failed` + `reason=no_processable_frames`: проверить входные данные и доступность источника.
- `partial` + `reason=corrupted_input`: высокий процент битых файлов.
- `partial` + `reason=detector_runtime_error`: ошибки рантайма детектора на кадрах.
- `partial` + `reason=mixed_input_and_detector_errors`: одновременно битые входы и ошибки детектора.
- Используется единый набор переменных `DB_DSN` и `ARTIFACTS_S3_*` для online и batch.
- Полный runbook: `docs/runbooks/batch_operations.md`.

## E2E Backfill сценарий

- Nightly workflow: `.github/workflows/batch-e2e.yml`.
- Сценарий поднимает платформу, seed'ит миссию, выполняет `airflow dags backfill rescue_batch_pipeline` и проверяет артефакты в S3 и строки в `batch_pipeline_metrics`.
- Плейбук real-data demo: `docs/runbooks/batch_demo_playbook.md`.
- Архитектурная схема: `docs/architecture/batch_contour.md`.
