#!/usr/bin/env bash
# Подъём наземной станции после перезагрузки ВМ/ноута.
#
# Что делает скрипт:
#   1. Ждёт, пока k3s отдаст API (k3s сам стартует — systemd unit `enabled`).
#   2. Ждёт под Vault и РАСПЕЧАТЫВАЕТ его 3-мя ключами из init-файла
#      (storage=file + shamir → при каждом рестарте Vault запечатывается).
#   3. Пересоздаёт под Vault Agent Injector — на старте он генерит свежий
#      webhook-cert (со временем он протухает, и новые поды создаются без
#      Vault Agent → виснут в Init без секретов).
#   4. Перезапускает не-Ready прикладные поды, чтобы они получили инъекцию
#      секретов и дошли до Ready.
#
# Запуск (с Mac, который рулит кластером):
#   KUBECONFIG=~/.kube/rescue-k3s.yaml ./scripts/ops/station_up.sh
#
# Переменные:
#   VAULT_NS        namespace Vault (по умолчанию vault)
#   VAULT_POD       имя пода Vault (по умолчанию rescue-ai-vault-0)
#   APP_NS          namespace приложения (по умолчанию rescue-ai)
#   INIT_FILE       путь к vault-init-*.json с unseal_keys_b64
set -euo pipefail

: "${VAULT_NS:=vault}"
: "${VAULT_POD:=rescue-ai-vault-0}"
: "${APP_NS:=rescue-ai}"
: "${INIT_FILE:=scripts/security/out/vault-init-offline.json}"

echo "==> 1/4 Жду k3s API…"
for _ in $(seq 1 60); do
  if kubectl get --raw=/healthz >/dev/null 2>&1; then break; fi
  sleep 2
done
kubectl get --raw=/healthz >/dev/null 2>&1 || { echo "k3s API недоступен"; exit 1; }
echo "    k3s готов."

echo "==> 2/4 Жду под Vault и распечатываю…"
for _ in $(seq 1 60); do
  if kubectl -n "$VAULT_NS" get pod "$VAULT_POD" >/dev/null 2>&1; then break; fi
  sleep 2
done

sealed() {
  kubectl -n "$VAULT_NS" exec "$VAULT_POD" -- vault status -format=json 2>/dev/null \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["sealed"])' 2>/dev/null
}

# Ждём, пока vault-сервер начнёт отвечать (даже запечатанный отвечает на status).
for _ in $(seq 1 60); do
  st="$(sealed || true)"
  [ "$st" = "True" ] || [ "$st" = "False" ] && break
  sleep 2
done

if [ "$(sealed || echo True)" = "False" ]; then
  echo "    Vault уже распечатан."
else
  command -v python3 >/dev/null || { echo "нужен python3 для чтения ключей"; exit 1; }
  [ -f "$INIT_FILE" ] || { echo "не найден $INIT_FILE с ключами"; exit 1; }
  # Берём первые 3 ключа (threshold=3) из 5.
  mapfile -t KEYS < <(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
for k in d["unseal_keys_b64"][:3]:
    print(k)
' "$INIT_FILE")
  for key in "${KEYS[@]}"; do
    kubectl -n "$VAULT_NS" exec "$VAULT_POD" -- vault operator unseal "$key" >/dev/null
  done
  [ "$(sealed)" = "False" ] && echo "    Vault распечатан." || { echo "не удалось распечатать"; exit 1; }
fi

echo "==> 3/4 Обновляю Vault Agent Injector (свежий webhook-cert)…"
# TLS-сертификат webhook'а инжектора со временем/после простоя может протухнуть
# (в логах: 'TLS handshake error: bad certificate'). Тогда API-сервер молча
# (failurePolicy=Ignore) создаёт НОВЫЕ поды БЕЗ Vault Agent → они виснут в Init
# без секретов. Пересоздаём под инжектора — на старте он генерит свежий cert и
# патчит caBundle webhook'а. Делаем ДО перезапуска прикладных подов.
INJ_POD="$(kubectl -n "$VAULT_NS" get pod \
  -l app.kubernetes.io/name=vault-agent-injector \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [ -n "$INJ_POD" ]; then
  kubectl -n "$VAULT_NS" delete pod "$INJ_POD" --wait=false >/dev/null 2>&1 || true
  kubectl -n "$VAULT_NS" wait --for=condition=ready pod \
    -l app.kubernetes.io/name=vault-agent-injector --timeout=120s >/dev/null 2>&1 \
    && echo "    инжектор готов (свежий cert)." \
    || echo "    ВНИМАНИЕ: инжектор не поднялся — проверь 'kubectl -n $VAULT_NS get pods'."
fi

echo "==> 4/4 Перезапускаю прикладные поды (чтобы получили инъекцию секретов)…"
# Любой под без readyReplicas мог стартовать без Vault Agent (пока cert был
# битый или Vault запечатан) — пересоздаём его.
for deploy in $(kubectl -n "$APP_NS" get deploy -o name); do
  name="${deploy#deployment.apps/}"
  ready="$(kubectl -n "$APP_NS" get "$deploy" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)"
  if [ "${ready:-0}" = "0" ]; then
    echo "    rollout restart $name"
    kubectl -n "$APP_NS" rollout restart "$deploy" >/dev/null
  fi
done

echo "==> Готово. Статус подов:"
kubectl -n "$APP_NS" get pods
