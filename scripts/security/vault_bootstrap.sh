#!/usr/bin/env bash
# Vault bootstrap для kind / k3s в dev-режиме (ADR-0008 §3).
#
# Что делает:
#  1. Включает KV v2 на пути `secret/`.
#  2. Включает Kubernetes auth method.
#  3. Загружает политики per-service.
#  4. Создаёт role'и `rescue-ai-api` и `rescue-ai-sync-worker`,
#     привязанные к ServiceAccount'ам этих подов.
#  5. Пишет в KV v2 примеры секретов из локальной .env.offline.
#
# Запуск:
#   VAULT_ADDR=http://localhost:8200 VAULT_TOKEN=root-dev-token \
#   ./scripts/security/vault_bootstrap.sh
#
# Для production-кластера значения VAULT_TOKEN и пути берутся из
# Vault unseal procedure (см. runbook docs/runbooks/vault_setup.md).

set -euo pipefail

: "${VAULT_ADDR:=http://localhost:8200}"
: "${VAULT_TOKEN:=root-dev-token}"
: "${NAMESPACE:=rescue-ai}"
: "${VAULT_NAMESPACE:=$NAMESPACE}"
: "${VAULT_SERVICE_ACCOUNT:=rescue-ai-vault}"

export VAULT_ADDR VAULT_TOKEN

echo "==> Vault status"
vault status >/dev/null

echo "==> Enable KV v2 at secret/"
vault secrets enable -path=secret -version=2 kv 2>/dev/null || \
    echo "    (already enabled)"

echo "==> Enable Kubernetes auth"
vault auth enable kubernetes 2>/dev/null || echo "    (already enabled)"

# Конфигурация k8s auth — Vault сам спросит kube-apiserver, кому
# принадлежит JWT-токен запрашивающего пода.
KUBE_HOST="https://kubernetes.default.svc"
TOKEN_REVIEWER_JWT="$(kubectl -n "$VAULT_NAMESPACE" create token "$VAULT_SERVICE_ACCOUNT" 2>/dev/null || \
    kubectl -n "$VAULT_NAMESPACE" get secret \
      "$(kubectl -n "$VAULT_NAMESPACE" get sa "$VAULT_SERVICE_ACCOUNT" -o jsonpath='{.secrets[0].name}')" \
      -o jsonpath='{.data.token}' | base64 -d)"
CA_CERT="$(kubectl config view --raw --minify --flatten \
    -o jsonpath='{.clusters[].cluster.certificate-authority-data}' | base64 -d)"

vault write auth/kubernetes/config \
    token_reviewer_jwt="$TOKEN_REVIEWER_JWT" \
    kubernetes_host="$KUBE_HOST" \
    kubernetes_ca_cert="$CA_CERT"

echo "==> Load policies"
vault policy write rescue-ai-base infra/k8s/vault/policies.hcl

echo "==> Bind roles to ServiceAccounts"
vault write auth/kubernetes/role/rescue-ai-api \
    bound_service_account_names=rescue-ai-rescue-ai-api \
    bound_service_account_namespaces="$NAMESPACE" \
    policies=rescue-ai-base \
    ttl=24h

vault write auth/kubernetes/role/rescue-ai-sync-worker \
    bound_service_account_names=rescue-ai-rescue-ai-sync-worker \
    bound_service_account_namespaces="$NAMESPACE" \
    policies=rescue-ai-base \
    ttl=24h

echo "==> Seed example secrets (KV v2)"
# Значения берутся из .env.offline / .env. Пользователь должен заранее
# проставить переменные в shell перед запуском, либо отредактировать
# тут вручную.
vault kv put secret/rescue-ai/api \
    DB_DSN="${DB_DSN:-postgresql://rescue:rescue-offline-dev@rescue-ai-postgresql:5432/rescue_ai}" \
    ARTIFACTS_S3_ACCESS_KEY_ID="${ARTIFACTS_S3_ACCESS_KEY_ID:-rescueadmin}" \
    ARTIFACTS_S3_SECRET_ACCESS_KEY="${ARTIFACTS_S3_SECRET_ACCESS_KEY:-rescueadmin}"

vault kv put secret/rescue-ai/sync-worker \
    DB_DSN="${DB_DSN:-postgresql://rescue:rescue-offline-dev@rescue-ai-postgresql:5432/rescue_ai}" \
    DEPLOYMENT_REMOTE_DB_DSN="${DEPLOYMENT_REMOTE_DB_DSN:-}" \
    DEPLOYMENT_REMOTE_S3_ACCESS_KEY_ID="${DEPLOYMENT_REMOTE_S3_ACCESS_KEY_ID:-}" \
    DEPLOYMENT_REMOTE_S3_SECRET_ACCESS_KEY="${DEPLOYMENT_REMOTE_S3_SECRET_ACCESS_KEY:-}"

echo
echo "Vault bootstrap done."
echo "Roles: rescue-ai-api, rescue-ai-sync-worker"
echo "Secrets:  secret/rescue-ai/api, secret/rescue-ai/sync-worker"
