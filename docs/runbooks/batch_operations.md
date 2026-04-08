# Batch Operations Runbook

## Статусы задач Airflow

- `success`: stage завершился успешно.
- `failed`: stage завершился с ошибкой.
- `up_for_retry`: stage ушел в retry.
- `skipped`: stage пропущен политикой выполнения.

## Диагностика

1. Проверить логи stage-задач `data/warmup/evaluate/publish` в Airflow.
2. Проверить JSON-артефакты stage в S3 (`dataset/model/evaluation`).
3. Проверить метрики в `batch_pipeline_metrics` (Postgres).

## Причины статусов

- `failed` + `reason=empty_input`: для даты запуска нет кадров.
- `failed` + `reason=no_processable_frames`: кадры есть, но ни один не обработан.
- `evaluate failed`: ошибки детектора на кадрах (`detector_errors > 0`).
- `publish failed`: ошибка записи в Postgres.

## Safe rerun

- Если stage-артефакт уже существует в S3, повторный запуск без `--force` вернет `idempotent_skip`.
- Для осознанного повтора после фикса входа запускать с `--force`.

Пример:

```bash
uv run python -m rescue_ai.interfaces.cli.batch --stage evaluate --mission-id demo_mission --ds 2026-03-01 --force
```

## Backfill

```bash
airflow dags backfill rescue_batch_daily -s 2026-03-01 -e 2026-03-05
```

После backfill проверить артефакты:

- `dataset/model/evaluation` JSON для каждой даты.
- upsert строк в `batch_pipeline_metrics` по ключу `(ds, mission_id, model_version, code_version)`.

## Детектор

- В batch-контуре используется `YoloDetector` для stage `warmup/evaluate`.
