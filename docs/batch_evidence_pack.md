# Batch Evidence Pack Checklist

Соберите перед сдачей:

1. Скрин DAG runs (`rescue_batch_daily`) с успешным backfill диапазоном.
2. Скрин history для повторного запуска без `--force` (idempotent skip).
3. Скрин таблицы `batch_pipeline_metrics` в Postgres с ключевыми метриками (`tp/fp/fn/accuracy/recall`).
4. Примеры `report.json` и `debug.csv` из artifact-store.
5. Скрин Prometheus/Grafana с метриками batch-прогонов.
6. Отчет по quality gates (`recall_event`, `fp_per_minute`, `ttfc_sec`) через `scripts/batch/check_report_quality.py`.
7. Ссылка на успешный nightly workflow `.github/workflows/batch-e2e.yml` (run URL + timestamp).
