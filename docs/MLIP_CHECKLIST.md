# ML in Prod Checklist

## Статус (2026-03-29)

- [x] Зависимости pinned через `uv` (`pyproject.toml` + `uv.lock`)
- [x] `Dockerfile` для сервиса
- [x] CI линтеров: `black`, `isort`, `flake8`, `mypy`, `pylint`
- [x] CI тестов
- [x] Push-based CD workflow (`.github/workflows/deploy.yml`)
- [x] Публикация двух образов в GHCR (`rescue-ai-online`, `rescue-ai-batch`)
- [x] Airflow DAG задеплоен в прод-контур (`rescue_batch_daily`, `@daily`, `catchup=true`)
- [x] mission_id для batch auto-discovery из S3 (без ручного `BATCH_MISSION_ID`)
- [x] Логическое разделение в одном Postgres: `app` schema (продуктовые таблицы) и `airflow` schema (метаданные Airflow)
- [x] Секреты через GitHub Secrets + `.env` на сервере
- [x] Online сервис использует Raspberry Pi source (`RPI_BASE_URL`) и проверку связи (`/rpi/status`)
- [x] Offline-first/sync-worker удалены из активного контура
- [x] Storage только удаленный: Postgres + S3-compatible
- [x] Batch source переведен на S3-only
- [x] REST API с минимальным публичным контуром:
  - [x] `GET /health`
  - [x] `GET /ready`
  - [x] `GET /rpi/status`
  - [x] `POST /predict/start`
  - [x] `GET /predict/{mission_id}`
  - [x] `POST /predict/{mission_id}/stop`
- [x] Swagger + OpenAPI YAML (`/openapi.yaml`, `docs/openapi.yaml`)

## Дальше перед защитой

1. Прогнать end-to-end demo со своим Raspberry Pi в прод-окружении.
2. Зафиксировать скриншоты/логи deploy и health/ready/rpi checks.
3. Подготовить короткий сценарий batch-run на реальных S3 данных.
