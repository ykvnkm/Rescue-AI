#!/usr/bin/env bash
# Разовая настройка cloud-Vault ОДНОЙ командой (security-инфра, отдельно
# от CI/CD приложения — её НЕЛЬЗЯ гонять на каждый деплой, иначе Vault
# пересоздаётся; см. docs/runbooks/vault_setup.md).
#
# Делает по шагам:
#   1. helm upgrade --install Vault (Yandex yckms-чарт + KMS auto-unseal);
#   2. vault operator init с recovery-ключами (если ещё не инициализирован);
#   3. ждёт auto-unseal через Yandex KMS (человек не нужен);
#   4. vault_bootstrap.sh — KV v2, k8s-auth, политики, роли, секреты.
#
# ЗАПУСК — на control-plane, из корня репозитория (~/rescue-ai):
#   1) положи рядом (оба gitignored, в scripts/security/out/):
#        - authorized_key.json   (SA-ключ Yandex для KMS)
#        - cloud.env             (cp scripts/security/cloud.env.example → заполни)
#   2) ./scripts/ops/cloud_bootstrap.sh
#
# Идемпотентно: повторный запуск не переинициализирует Vault, а лишь
# до-применит чарт и bootstrap (политики/секреты перезапишутся значениями
# из cloud.env — это норма, не skip-by-exists).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

: "${KUBECONFIG:=/etc/rancher/k3s/k3s.yaml}"; export KUBECONFIG
VAULT_NS="vault"
VAULT_POD="rescue-ai-vault-0"
SA_KEY="scripts/security/out/authorized_key.json"
ENV_FILE="scripts/security/out/cloud.env"
INIT_FILE="scripts/security/out/vault-init-cloud.json"
CHART="oci://cr.yandex/yc-marketplace/yandex-cloud/vault/chart/vault"
CHART_VER="0.30.0-3-yckms"

# ── предусловия ───────────────────────────────────────────────────────
[ -f "$SA_KEY" ]   || { echo "нет $SA_KEY (SA-ключ Yandex для KMS)"; exit 1; }
[ -f "$ENV_FILE" ] || { echo "нет $ENV_FILE — cp scripts/security/cloud.env.example и заполни"; exit 1; }
command -v helm >/dev/null    || { echo "нет helm"; exit 1; }
command -v kubectl >/dev/null || { echo "нет kubectl"; exit 1; }
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a
: "${YC_KMS_KEY_ID:?YC_KMS_KEY_ID не задан в cloud.env}"
: "${DB_DSN:?DB_DSN не задан в cloud.env}"

vexec() { kubectl -n "$VAULT_NS" exec "$VAULT_POD" -- sh -c "export VAULT_ADDR=http://127.0.0.1:8200; $1"; }

echo "==> 1/4 helm upgrade --install Vault (yckms + KMS auto-unseal)"
kubectl create namespace "$VAULT_NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
helm upgrade --install rescue-ai-vault "$CHART" --version "$CHART_VER" \
    -n "$VAULT_NS" \
    -f infra/k8s/vault/values-cloud.yaml \
    -f infra/k8s/vault/values-cloud-kms.yaml \
    --set server.extraEnvironmentVars.YANDEXCLOUD_KMS_KEY_ID="$YC_KMS_KEY_ID" \
    --set-file yandexKmsAuthJson="$SA_KEY"

echo "==> жду, пока vault-под примет запросы (даже запечатанный отвечает на status)"
for _ in $(seq 1 40); do
  vexec 'vault status >/dev/null 2>&1; echo ok' 2>/dev/null | grep -q ok && break
  # 'vault status' возвращает код 2 если запечатан — нам важно, что под жив
  kubectl -n "$VAULT_NS" get pod "$VAULT_POD" -o jsonpath='{.status.phase}' 2>/dev/null | grep -q Running && \
    vexec 'vault status' >/dev/null 2>&1 && break
  sleep 4
done

echo "==> 2/4 init (recovery-ключи) если ещё не инициализирован"
if vexec 'vault status -format=json 2>/dev/null' | python3 -c 'import sys,json;print(json.load(sys.stdin)["initialized"])' 2>/dev/null | grep -qi true; then
  echo "    Vault уже инициализирован — пропускаю init"
else
  echo "    инициализирую с -recovery-shares=5 -recovery-threshold=3"
  vexec 'vault operator init -recovery-shares=5 -recovery-threshold=3 -format=json' > "$INIT_FILE"
  chmod 600 "$INIT_FILE"
  echo "    recovery-ключи + root-токен сохранены в $INIT_FILE (положи в 1Password!)"
fi
[ -f "$INIT_FILE" ] || { echo "нет $INIT_FILE с root-токеном — не могу делать bootstrap"; exit 1; }
ROOT_TOKEN="$(python3 -c 'import json;print(json.load(open("'"$INIT_FILE"'"))["root_token"])')"

echo "==> 3/4 жду auto-unseal через Yandex KMS"
ok=""
for _ in $(seq 1 30); do
  [ "$(kubectl -n "$VAULT_NS" get pod "$VAULT_POD" -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null)" = "true" ] && { ok=1; break; }
  sleep 5
done
[ -n "$ok" ] || { echo "Vault не распечатался — проверь доступ к Yandex KMS: kubectl -n $VAULT_NS logs $VAULT_POD"; exit 1; }
vexec 'vault status' 2>/dev/null | grep -iE "Seal Type|Sealed" | sed 's/^/    /'

echo "==> 4/4 bootstrap: KV v2, k8s-auth, политики, роли, секреты"
# vault_bootstrap.sh работает через vault CLI + port-forward.
kubectl -n "$VAULT_NS" port-forward "svc/rescue-ai-vault" 8200:8200 >/dev/null 2>&1 &
PF_PID=$!
trap 'kill $PF_PID 2>/dev/null || true' EXIT
sleep 4
# vault CLI: берём системный, иначе вытаскиваем из пода
VAULT_BIN="$(command -v vault || true)"
if [ -z "$VAULT_BIN" ]; then
  kubectl -n "$VAULT_NS" exec "$VAULT_POD" -- cat /bin/vault > /tmp/vault && chmod +x /tmp/vault
  VAULT_BIN=/tmp/vault
fi
PATH="$(dirname "$VAULT_BIN"):$PATH"; export PATH

VAULT_ADDR=http://127.0.0.1:8200 \
VAULT_TOKEN="$ROOT_TOKEN" \
NAMESPACE=rescue-ai \
VAULT_NAMESPACE="$VAULT_NS" \
VAULT_SERVICE_ACCOUNT=rescue-ai-vault \
MONITORING_NAMESPACE=monitoring \
ENABLE_LOCAL_INFRA=false \
ENABLE_BATCH_EXPORTER=true \
ENABLE_AIRFLOW=true \
ENABLE_ALERTMANAGER=true \
ENABLE_GRAFANA=true \
    bash scripts/security/vault_bootstrap.sh

# Cloud-демо открытое — API_AUTH_TOKEN пустой (иначе закроется публичный доступ)
VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN="$ROOT_TOKEN" \
    "$VAULT_BIN" kv patch secret/rescue-ai/api API_AUTH_TOKEN='' >/dev/null 2>&1 || true

echo "==> Готово. Vault настроен (auto-unseal через Yandex KMS)."
echo "    Дальше — деплой приложения через CI/CD (merge в main → deploy.yml)."
