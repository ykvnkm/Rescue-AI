# Vault policy для пода rescue-ai-sync-worker (ADR-0008 §3).
#
# Sync-worker дренирует локальный outbox в удалённую инфраструктуру.
# Нужен DSN удалённой Postgres и ключи удалённого S3 — отдельно от
# секретов api, чтобы оба сервиса могли быть скомпрометированы
# независимо.

path "secret/data/rescue-ai/sync-worker" {
  capabilities = ["read"]
}
