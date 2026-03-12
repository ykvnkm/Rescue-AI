# Platform Skeleton

Инфраструктурный каркас для локального dev/stage-стенда:

- `Postgres` + `postgres-exporter`
- `MinIO` (S3-совместимое хранилище)
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
- MinIO API: `http://localhost:9000`
- MinIO Console: `http://localhost:9001`

## Остановка

```bash
docker compose -f docker-compose.platform.yml --env-file platform.env down
```

## Важно

- В `platform.env.example` заданы dev-учетки, для shared/stage меняйте все пароли.
- `infra/postgres/init/001-init-platform.sql` задает стартовые БД/пользователей (`airflow`, `rescue_app`, `postgres_exporter`).
- Дашборд Grafana загружается автоматически из `infra/grafana/dashboards/platform-overview.json`.
- Пример DAG находится в `infra/airflow/dags/healthcheck_dag.py`.
- DAG с `DockerOperator` и бизнес batch-runner: `infra/airflow/dags/idempotent_docker_backfill_demo.py`.

## DAG: Rescue Batch (DockerOperator + Idempotency + Backfill)

`rescue_batch_daily`:

- Запускается ежедневно (`@daily`) с `catchup=True`.
- Оркестрирует только запуск runner-контейнера.
- Бизнес-логика вынесена в `libs/batch/application/mission_batch_runner.py`.
- Idempotency key: `(mission_id, ds, config_hash, model_version)`.
- Статусы процесса: `running/completed/failed/partial`.

Канонический контракт запуска batch-runner:

- Вход: `mission_id`, `ds`, `model_version`, `code_version`, `force`.
- Выход: `status`, `report_uri`, `debug_uri`, `run_key`.

Сборка образа для `DockerOperator`:

```bash
docker compose -f docker-compose.platform.yml --env-file platform.env --profile batch-build build batch-runner-image
```

Backfill за диапазон дат:

```bash
docker compose -f docker-compose.platform.yml --env-file platform.env exec airflow-webserver \
  airflow dags backfill rescue_batch_daily -s 2026-03-01 -e 2026-03-03
```

Проверка batch-статусов и артефактов:

```bash
docker compose -f docker-compose.platform.yml --env-file platform.env exec airflow-webserver \
  ls -la /opt/airflow/data/status /opt/airflow/data/artifacts
```

Проверка идемпотентности:

```bash
docker compose -f docker-compose.platform.yml --env-file platform.env exec airflow-webserver \
  uv run python -m services.batch_runner.main --mission-id demo_mission --ds 2026-03-01

docker compose -f docker-compose.platform.yml --env-file platform.env exec airflow-webserver \
  uv run python -m services.batch_runner.main --mission-id demo_mission --ds 2026-03-01
```

Второй запуск (без `--force`) должен вернуть `idempotent_skip`.

Force rerun:

```bash
docker compose -f docker-compose.platform.yml --env-file platform.env exec airflow-webserver \
  uv run python -m services.batch_runner.main --mission-id demo_mission --ds 2026-03-01 --force
```

## Runbook (failed/partial)

- `failed` + `reason=empty_input`: проверить путь `BATCH_MISSION_ROOT/<mission_id>/<ds>/images`.
- `failed` + `reason=no_processable_frames`: проверить битые кадры/формат изображений.
- `partial` + `reason=high_corrupted_frame_ratio`: проверить качество входных данных и повторить с `--force` после исправления.
- Для shared/stage использовать `BATCH_RUNTIME_ENV=staging`, тогда по умолчанию включаются `PostgresStatusStore` и `S3ArtifactStore`.
- Полный runbook: `docs/runbooks/batch_operations.md`.
- `LocalMissionSource` помечает кадр как `is_corrupted=True`, если сигнатура изображения не распознаётся (`jpeg/png/bmp/webp`).
