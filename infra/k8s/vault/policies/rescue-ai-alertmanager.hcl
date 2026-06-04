# Vault policy для пода Alertmanager (ADR-0008 §3, диплом §3.5.4).
#
# Поду разрешено читать только собственный KV-путь с SMTP-credentials
# и адресами получателей (on-call email warning + critical). Никакого
# доступа к credentials приложения или Grafana — принцип least
# privilege.

path "secret/data/rescue-ai/alertmanager" {
  capabilities = ["read"]
}
