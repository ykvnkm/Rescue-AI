# Batch Operations Runbook

## Пайплайн

DAG `rescue_batch_pipeline` запускает три стадии последовательно:

1. `prepare_dataset` — собирает манифест датасета из кадров и лейблов миссии.
2. `evaluate_model` — прогоняет детектор по манифесту, пишет confusion matrix.
3. `publish_metrics` — апсертит одну строку на миссию в `batch_pipeline_metrics`.

## Статусы задач Airflow

- `success`: stage завершился успешно.
- `failed`: stage завершился с ошибкой.
- `up_for_retry`: stage ушёл в retry.

## Диагностика

1. Проверить логи stage-задач `prepare_dataset / evaluate_model / publish_metrics` в Airflow.
2. Проверить JSON-артефакты в S3:
   `{prefix}/batch/ml_pipeline/ds=YYYY-MM-DD/mission={id}/{dataset,evaluation_<mv>_<cv>}.json`.
3. Проверить строки в `batch_pipeline_metrics` (Postgres) по
   `(ds, mission_id, model_version, code_version)`.

## Rerun

Rerun через clear таски в Airflow UI всегда безопасен:

- `prepare_dataset` и `evaluate_model` перезаписывают свои JSON-артефакты в S3
  (атомарный `put_object`).
- `publish_metrics` делает `INSERT ... ON CONFLICT ... DO UPDATE` на ключе
  `(ds, mission_id, model_version, code_version)`.
- Discovery миссий пересчитывается на каждом запуске через свежий
  `list_objects_v2`, так что новые миссии, добавленные между прогонами,
  подхватятся автоматически.

## Backfill

```bash
airflow dags backfill rescue_batch_pipeline -s 2026-03-01 -e 2026-03-05
```

После backfill проверить, что для каждой `ds` лежат оба JSON-артефакта и
появились строки в `batch_pipeline_metrics`.
