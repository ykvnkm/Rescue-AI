# Batch Demo Playbook (Real Data, YOLO)

## Цель
Показать end-to-end batch-контур на реальных данных: backfill, rerun,
артефакты в S3 и сводные метрики в Postgres.

## Предусловия
- В S3 по префиксу `{ARTIFACTS_S3_PREFIX}/ds=YYYY-MM-DD/{mission_id}/`
  лежат `frames/*.jpg` и `labels.json`.
- Платформа поднята (`infra/docker-compose.platform.yml`).

## Рекомендуемый сценарий
- `date range`: `2026-03-01..2026-03-03`
- Запуск через Airflow backfill.

## Команды

```bash
cd infra
cp platform.env.example platform.env

docker compose -f docker-compose.platform.yml --env-file platform.env up -d

docker compose -f docker-compose.platform.yml --env-file platform.env exec airflow-webserver \
  airflow dags backfill rescue_batch_pipeline -s 2026-03-01 -e 2026-03-03
```

## Проверка артефактов

В S3 должны появиться для каждой `(ds, mission)`:

- `{prefix}/batch/ml_pipeline/ds=<ds>/mission=<id>/dataset.json`
- `{prefix}/batch/ml_pipeline/ds=<ds>/mission=<id>/evaluation.json`

В Postgres — одна строка на `(ds, mission_id)` в таблице
`batch_pipeline_metrics`.

## Проверка rerun

Повторный clear любой таски в Airflow UI за ту же `ds` должен успешно
перезаписать артефакты в S3 и апсертнуть строку в Postgres —
skip-by-exists в пайплайне нет.
