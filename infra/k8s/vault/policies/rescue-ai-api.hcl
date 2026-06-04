# Vault policy для пода rescue-ai-api (ADR-0008 §3).
#
# Привязывается к k8s ServiceAccount-у через role `rescue-ai-api`
# (см. scripts/security/vault_bootstrap.sh). Поду разрешено читать
# только собственные KV-пути — runtime env и mTLS material для канала
# к Raspberry Pi. К секретам sync-worker'а, batch-exporter'а и infra
# storage pod не имеет доступа.

path "secret/data/rescue-ai/api" {
  capabilities = ["read"]
}

path "secret/data/rescue-ai/rpi-mtls" {
  capabilities = ["read"]
}
