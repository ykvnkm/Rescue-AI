# Vault policies for Rescue-AI services (ADR-0008 §3).
#
# Применяются скриптом scripts/security/vault_bootstrap.sh после первого
# запуска Vault. Каждая роль ограничена только своим путём в KV v2 —
# api не может читать секреты sync-worker'а и наоборот.

# ── rescue-ai-api ──────────────────────────────────────────────────
path "secret/data/rescue-ai/api" {
  capabilities = ["read"]
}

# ── rescue-ai-sync-worker ──────────────────────────────────────────
path "secret/data/rescue-ai/sync-worker" {
  capabilities = ["read"]
}

# ── rescue-ai-batch-exporter ──────────────────────────────────────
# Микросервис, читающий таблицу batch_pipeline_metrics. Нужен только
# DSN базы данных.
path "secret/data/rescue-ai/batch-exporter" {
  capabilities = ["read"]
}
