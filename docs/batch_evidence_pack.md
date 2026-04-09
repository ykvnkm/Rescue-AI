# Batch Evidence Pack Checklist

Соберите перед сдачей:

1. Скрин DAG runs (`rescue_batch_pipeline`) с успешным backfill диапазоном.
2. Скрин history для повторного clear таски — артефакты перезаписаны, строка в Postgres обновлена.
3. Скрин таблицы `batch_pipeline_metrics` в Postgres с ключевыми метриками (`tp/fp/fn/accuracy/precision/recall`).
4. Примеры `dataset.json` и `evaluation_<mv>_<cv>.json` из S3.
5. Скрин Prometheus/Grafana с метриками batch-прогонов.
6. Ссылка на успешный nightly workflow `.github/workflows/batch-e2e.yml` (run URL + timestamp).
