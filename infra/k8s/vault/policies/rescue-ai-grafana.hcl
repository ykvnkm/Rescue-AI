# Vault policy для пода Grafana (ADR-0008 §3, диплом §3.5.4).
#
# Поду разрешено читать только собственный KV-путь с admin-учёткой.
# Никакого доступа к секретам приложения (api/sync-worker/...) или
# к alertmanager-у. Принцип least privilege.

path "secret/data/rescue-ai/grafana" {
  capabilities = ["read"]
}
