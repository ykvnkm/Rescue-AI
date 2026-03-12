# Platform Skeleton

Инфраструктурный каркас для локального dev/stage-стенда:

- `Postgres` + `postgres-exporter`
- `Airflow` (`webserver`, `scheduler`, `init`)
- `Prometheus`
- `Grafana` (datasource и dashboard provisioning)

## Быстрый старт

```bash
cd infra
cp platform.env.example platform.env
docker compose -f docker-compose.platform.yml --env-file platform.env up -d
```

UI/Endpoints:

- Airflow: `http://localhost:8080`
- Grafana: `http://localhost:3000`
- Prometheus: `http://localhost:9090`

## Остановка

```bash
docker compose -f docker-compose.platform.yml --env-file platform.env down
```

## Важно

- В `platform.env.example` нет дефолтных секретов. Перед запуском заполните все `*_PASSWORD`, S3 ключи и логины UI.
- `infra/postgres/init/001-init-platform.sh` создает стартовые БД/пользователей (`airflow`, `POSTGRES_APP_USER`) из переменных окружения.
- Дашборд Grafana загружается автоматически из `infra/grafana/dashboards/platform-overview.json`.
- Основной DAG batch-контура: `infra/airflow/dags/idempotent_docker_backfill_demo.py`.

## DAG: Rescue Batch (DockerOperator + Idempotency + Backfill)

`rescue_batch_daily`:

- Запускается ежедневно (`@daily`) с `catchup=True`.
- Оркестрирует запуск runner-контейнера.
- Бизнес-логика вынесена в `libs/batch/application/mission_batch_runner.py`.
- Idempotency key: `(mission_id, ds, config_hash, model_version)`.
- Статусы процесса: `running/completed/failed/partial`.

Канонический контракт запуска batch-runner:

- Вход: `mission_id`, `ds`, `model_version`, `code_version`, `force`.
- Выход: `status`, `report_uri`, `debug_uri`, `run_key`.

## Пошаговый запуск Airflow и что смотреть

1. Подготовьте `infra/platform.env`:
```bash
cp infra/platform.env.example infra/platform.env
```
Заполните минимум:
`POSTGRES_ADMIN_USER`, `POSTGRES_ADMIN_PASSWORD`, `POSTGRES_AIRFLOW_PASSWORD`, `POSTGRES_APP_PASSWORD`, `AIRFLOW_ADMIN_USER`, `AIRFLOW_ADMIN_PASSWORD`, `AIRFLOW_ADMIN_EMAIL`, `GRAFANA_ADMIN_USER`, `GRAFANA_ADMIN_PASSWORD`, `ARTIFACTS_S3_ENDPOINT`, `ARTIFACTS_S3_REGION`, `ARTIFACTS_S3_ACCESS_KEY_ID`, `ARTIFACTS_S3_SECRET_ACCESS_KEY`, `ARTIFACTS_S3_BUCKET`.

2. Проверьте конфиг compose:
```bash
docker compose -f infra/docker-compose.platform.yml --env-file infra/platform.env config -q
```

3. Соберите образ для DockerOperator:
```bash
docker compose -f infra/docker-compose.platform.yml --env-file infra/platform.env --profile batch-build build batch-runner-image
```

4. Поднимите платформу:
```bash
docker compose -f infra/docker-compose.platform.yml --env-file infra/platform.env up -d
```

5. Откройте Airflow UI (`http://localhost:8080`), включите DAG `rescue_batch_daily`, зайдите в Graph/Grid.

6. Запустите backfill за диапазон:
```bash
docker compose -f infra/docker-compose.platform.yml --env-file infra/platform.env exec airflow-webserver \
  airflow dags backfill rescue_batch_daily -s 2026-03-10 -e 2026-03-12
```

7. Проверьте артефакты и статусы:
```bash
docker compose -f infra/docker-compose.platform.yml --env-file infra/platform.env exec airflow-webserver \
  ls -la /opt/airflow/data/status /opt/airflow/data/artifacts
```

8. Проверьте идемпотентность: повторите запуск на ту же дату без `--force` и убедитесь, что статус `idempotent_skip`.

## Runbook (failed/partial)

- `failed` + `reason=empty_input`: проверить путь `BATCH_MISSION_ROOT/<mission_id>/<ds>/images`.
- `failed` + `reason=no_processable_frames`: проверить входные данные и доступность источника.
- `partial` + `reason=corrupted_input`: высокий процент битых файлов.
- `partial` + `reason=detector_runtime_error`: ошибки рантайма детектора на кадрах.
- `partial` + `reason=mixed_input_and_detector_errors`: одновременно битые входы и ошибки детектора.
- Для shared/stage использовать `BATCH_RUNTIME_ENV=staging`, тогда по умолчанию включаются `PostgresStatusStore` и `S3ArtifactStore`.
- Не задавайте `BATCH_ARTIFACT_BACKEND` и `BATCH_STATUS_BACKEND`, если хотите использовать runtime-defaults по `BATCH_RUNTIME_ENV`.
- Для S3 используйте единый набор переменных `ARTIFACTS_S3_*`; `BATCH_S3_*` поддерживается как совместимый override.
- Полный runbook: `docs/runbooks/batch_operations.md`.

## E2E Backfill в CI

- Nightly workflow: `.github/workflows/batch-e2e.yml`.
- Сценарий поднимает платформу, seed'ит миссию, выполняет `airflow dags backfill rescue_batch_daily` и проверяет status/artifacts.
- Плейбук real-data demo: `docs/runbooks/batch_demo_playbook.md`.
- Архитектурная схема: `docs/architecture/batch_contour.md`.
