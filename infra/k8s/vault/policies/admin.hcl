# Vault policy `admin` — для человека-администратора (вход в UI по userpass).
#
# Назначается пользователю userpass (см. vault_setup.md / cloud_bootstrap.sh).
# Root-токен после этого убирается в 1Password и используется только для
# аварийного доступа (break-glass). Повседневное управление секретами —
# под этой политикой через UI https://vault.rescue-ai.ru.

# KV-секреты приложения (создание/чтение/правка/удаление).
path "secret/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

# Управление ACL-политиками.
path "sys/policies/acl"   { capabilities = ["list"] }
path "sys/policies/acl/*" { capabilities = ["create", "read", "update", "delete", "list"] }

# Управление auth-методами и секрет-движками.
path "sys/auth"     { capabilities = ["read", "list"] }
path "sys/auth/*"   { capabilities = ["create", "read", "update", "delete", "sudo"] }
path "auth/*"       { capabilities = ["create", "read", "update", "delete", "list", "sudo"] }
path "sys/mounts"   { capabilities = ["read", "list"] }
path "sys/mounts/*" { capabilities = ["create", "read", "update", "delete", "list"] }

# Состояние и UI-рендеринг.
path "sys/health"            { capabilities = ["read"] }
path "sys/seal-status"       { capabilities = ["read"] }
path "sys/capabilities-self" { capabilities = ["update"] }
path "sys/internal/ui/*"     { capabilities = ["read"] }
