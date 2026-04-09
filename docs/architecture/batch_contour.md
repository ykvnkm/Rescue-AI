# Batch Architecture Contour

```mermaid
flowchart LR
  A[Airflow DAG rescue_batch_pipeline\nDockerOperator x3] --> B[rescue_ai.interfaces.cli.batch]
  B --> C[S3MissionSource]
  B --> D[YoloDetector]
  B --> E[S3StageStore]
  B --> F[PostgresBatchMetricsRepository]

  E --> G[(dataset.json / evaluation_*.json in S3)]
  F --> H[(batch_pipeline_metrics)]
```

Принцип:
- DAG оркестрирует три таски: `prepare_dataset -> evaluate_model -> publish_metrics`.
- Бизнес-логика стадий живёт в `rescue_ai.application.pipeline_stages`.
- `publish_metrics` апсертит итоговые метрики в `batch_pipeline_metrics`.
