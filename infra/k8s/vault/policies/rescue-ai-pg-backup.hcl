# Vault policy для CronJob backup'а локальной Postgres (ADR-0008 §4).
#
# Pod нужны два набора credentials: пароль постгреса (для pg_dump)
# и root MinIO (для mc cp в локальный bucket). Оба пути read-only;
# никакой write capability — это исключает использование роли для
# подмены пароля.

path "secret/data/rescue-ai/postgresql" {
  capabilities = ["read"]
}

path "secret/data/rescue-ai/minio" {
  capabilities = ["read"]
}
