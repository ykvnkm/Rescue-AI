#!/usr/bin/env bash
# Подъём облачного кластера (reg.ru, 3 ноды) после остановки/запуска серверов.
#
# Что происходит при остановке VPS и обратном запуске:
#   - k3s на всех нодах стартует сам (systemd unit `enabled`); воркеры
#     переподключаются к control-plane автоматически — руками ничего не надо.
#   - Vault настроен на AUTO-UNSEAL через Yandex Cloud KMS (seal type
#     yandexcloudkms): при старте пода он сам идёт в Yandex KMS и
#     распечатывается БЕЗ человека и без ключей. Ручная распечатка больше
#     НЕ нужна. (Shamir-ключи из vault-init-cloud.json теперь являются
#     recovery-ключами — нужны лишь для редких операций: rekey, generate-root.)
#
# Этот скрипт:
#   1. ждёт, пока control-plane отдаст k3s API;
#   2. ждёт, пока Vault сам распечатается (Ready) через KMS;
#   3. перезапускает прикладные поды, которые стартовали раньше Vault.
#
# Запуск:
#   ./scripts/ops/cloud_up.sh
#
# Переменные:
#   CP_HOST    публичный IP control-plane (обязательно задать)
#   SSH_KEY    приватный SSH-ключ для входа под rescue
set -euo pipefail

: "${CP_HOST:?задайте CP_HOST — публичный IP control-plane}"
: "${SSH_KEY:=$HOME/.ssh/id_ed25519}"
CP="rescue@${CP_HOST}"

cp_ssh() { ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=12 "$CP" "$@"; }

echo "==> 1/3 Жду control-plane (${CP_HOST})…"
for _ in $(seq 1 40); do
  if cp_ssh 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; kubectl get --raw=/healthz' >/dev/null 2>&1; then
    break
  fi
  sleep 4
done
cp_ssh 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; kubectl get --raw=/healthz' >/dev/null 2>&1 \
  || { echo "control-plane недоступен — проверь, что VPS включён и SSH работает"; exit 1; }
echo "    k3s готов. Ноды:"
cp_ssh 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; kubectl get nodes --no-headers' 2>/dev/null | awk '{print "      "$1, $2}'

echo "==> 2/3 Жду auto-unseal Vault через Yandex KMS…"
ok=""
for _ in $(seq 1 30); do
  ready="$(cp_ssh 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; kubectl -n vault get pod rescue-ai-vault-0 -o jsonpath="{.status.containerStatuses[0].ready}" 2>/dev/null' 2>/dev/null || true)"
  if [ "$ready" = "true" ]; then ok=1; break; fi
  sleep 5
done
if [ -n "$ok" ]; then
  echo "    Vault распечатан KMS-ом автоматически:"
  cp_ssh 'kubectl -n vault exec rescue-ai-vault-0 -- sh -c "export VAULT_ADDR=http://127.0.0.1:8200; vault status"' 2>/dev/null \
    | grep -iE "Seal Type|Sealed" | sed 's/^/      /'
else
  echo "    Vault не стал Ready за отведённое время — проверь доступность Yandex KMS"
  echo "    и логи: kubectl -n vault logs rescue-ai-vault-0"
  exit 1
fi

echo "==> 3/3 Перезапускаю не-Ready прикладные поды…"
cp_ssh '
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
for ns in rescue-ai monitoring rescue-batch; do
  kubectl get ns "$ns" >/dev/null 2>&1 || continue
  for d in $(kubectl -n "$ns" get deploy -o name 2>/dev/null); do
    r=$(kubectl -n "$ns" get "$d" -o jsonpath="{.status.readyReplicas}" 2>/dev/null || echo 0)
    if [ "${r:-0}" = "0" ]; then echo "    rollout restart $ns/$d"; kubectl -n "$ns" rollout restart "$d" >/dev/null; fi
  done
done'

echo "==> Готово. Статус:"
cp_ssh 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; kubectl get pods -A 2>/dev/null | grep -E "rescue-ai|vault|monitoring|rescue-batch|ingress-nginx"'
echo ""
echo "Проверка: https://api.rescue-ai.ru/health  должен отдать {\"status\":\"ok\"}"
