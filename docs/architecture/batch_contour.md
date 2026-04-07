# Batch Architecture Contour

```mermaid
flowchart LR
  A[Airflow DAG rescue_batch_daily\nDockerOperator] --> B[rescue_ai.interfaces.cli.batch]
  B --> C[S3MissionSource]
  B --> D[YoloDetector]
  B --> E[S3StageStore]
  B --> F[PostgresBatchMetricsRepository]

  E --> G[(dataset/model/validation JSON in S3)]
  F --> H[(batch_pipeline_metrics)]
```

Принцип:
- DAG только оркестрирует запуск.
- Бизнес-логика stage-пайплайна живет в `pipeline_stages`.
- `publish` пишет итоговые метрики напрямую в `batch_pipeline_metrics`.
