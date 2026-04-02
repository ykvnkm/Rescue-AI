# Batch: Backfill, Идемпотентность, Хранение в S3/Postgres

## Как теперь работает DAG

- `discover_missions` находит миссии только для текущей даты `ds`.
- Обучение всегда запускается глобально на `mission_id=__all_missions__`.
- Порядок выполнения в одном основном потоке:
  - `stage_data(__all_missions__)`
  - `stage_train(__all_missions__)`
  - `stage_validate(__all_missions__)`
  - `discover_missions(ds)`
  - `stage_inference(<mission_id>)` для каждой найденной миссии.

Итог: на каждом `ds` модель обучается на всех данных с датой `<= ds` (история + новые данные), а не только на текущем дне.

## Как показать backfill

1. Запустить backfill:
```bash
airflow dags backfill rescue_batch_daily -s 2026-03-01 -e 2026-03-03
```
2. Проверить, что есть глобальные артефакты обучения для каждой даты:
   - `.../ml_pipeline/mission=__all_missions__/ds=2026-03-01/...`
   - `.../ml_pipeline/mission=__all_missions__/ds=2026-03-02/...`
   - `.../ml_pipeline/mission=__all_missions__/ds=2026-03-03/...`
3. В `dataset.json` сравнить `rows_total` между датами:
   - для `2026-03-02` значение должно быть `>= 2026-03-01`;
   - для `2026-03-03` значение должно быть `>= 2026-03-02`.

## Как показать идемпотентность

1. Повторно запустить ту же дату без `--force`.
2. Ожидаемое поведение:
   - stage-артефакты уже существуют по тем же детерминированным ключам;
   - шаги возвращают `status=idempotent_skip`.
3. `--force` использовать только для осознанного пересчёта.

## Рекомендуемое разбиение S3 по датам

Использовать две зоны:

1. Неизменяемые исходные данные:
   - `<prefix>/mission=<mission_id>/ds=<YYYY-MM-DD>/images/...`
   - `<prefix>/mission=<mission_id>/ds=<YYYY-MM-DD>/annotations/...`

2. Детерминированные ML-артефакты:
   - `<prefix>/batch/ml_pipeline/mission=<mission_scope>/ds=<YYYY-MM-DD>/dataset.json`
   - `<prefix>/batch/ml_pipeline/mission=<mission_scope>/ds=<YYYY-MM-DD>/model_*.json`
   - `<prefix>/batch/ml_pipeline/mission=<mission_scope>/ds=<YYYY-MM-DD>/validation_*.json`
   - `<prefix>/batch/ml_pipeline/mission=<mission_scope>/ds=<YYYY-MM-DD>/inference_*.json`

Где `mission_scope`:
- либо конкретная миссия;
- либо `__all_missions__` для глобального обучения.

## Что хранить в Postgres

Рекомендуемый минимум в БД приложения:

- `missions`
- `frame_events`
- `alerts`
- `episodes`

Статусы оркестрации и идемпотентность лучше считать через S3-артефакты и метаданные Airflow.

По таблице `batch_mission_runs`:
- если отдельная SQL-отчётность по статусам не нужна, таблица может считаться опциональной;
- в таком варианте источник истины по batch-статусам: S3 (`inference_*.json`, `report.json`, `debug.csv`) + Airflow.
