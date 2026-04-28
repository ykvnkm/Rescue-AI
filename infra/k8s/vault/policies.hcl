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

# ── rescue-ai-batch (P4) ───────────────────────────────────────────
# path "secret/data/rescue-ai/batch" {
#   capabilities = ["read"]
# }
