# Vault policy для пода rescue-ai-batch-exporter (ADR-0008 §3).
#
# Batch-exporter раз в N минут читает таблицу batch_pipeline_metrics
# в локальной Postgres и публикует gauge'и для Prometheus. Нужен
# только DSN прикладной Postgres.

path "secret/data/rescue-ai/batch-exporter" {
  capabilities = ["read"]
}
