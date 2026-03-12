# Batch Operations Runbook

## Статусы

- `running`: обработка активна.
- `completed`: run завершен успешно.
- `partial`: run завершен частично (обычно высокий процент битых кадров).
- `failed`: run завершен с ошибкой.

## Диагностика

1. Проверить run status в `batch_mission_runs` (Postgres) или `runs.json` (local).
2. Проверить `reason` и `quality.error_rate` в `report.json`.
3. Проверить наличие `debug.csv` и долю `status=corrupted|detection_error`.

## Причины статусов

- `failed` + `reason=empty_input`: для даты запуска нет кадров.
- `failed` + `reason=no_processable_frames`: кадры есть, но ни один не обработан.
- `partial` + `reason=corrupted_input`: битые/невалидные входные файлы.
- `partial` + `reason=detector_runtime_error`: ошибки детектора при inference.
- `partial` + `reason=mixed_input_and_detector_errors`: комбинация проблем входа и inference.

## Safe rerun

- Если ключ `(mission_id, ds, config_hash, model_version)` уже `completed`, повторный запуск без `--force` должен быть skip.
- Для осознанного повтора после фикса входа запускать с `--force`.

Пример:

```bash
uv run python -m services.batch_runner.main --mission-id demo_mission --ds 2026-03-01 --force
```

## Backfill

```bash
airflow dags backfill rescue_batch_daily -s 2026-03-01 -e 2026-03-05
```

После backfill проверить артефакты:

- `report.json` и `debug.csv` для каждой даты.
- отсутствие дубликатов status-строк по `run_key`.

## Детектор

- В batch-контуре доступен только реальный `YoloDetectionRuntime`.
