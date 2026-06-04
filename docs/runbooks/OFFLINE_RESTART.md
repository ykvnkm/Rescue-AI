# --- 0. С Mac: указать на кластер (поправь IP, если ВМ сменила его по DHCP) ---
export KUBECONFIG=~/.kube/rescue-k3s.yaml
kubectl get nodes                      # нода Ready? если "connection refused" — поправь server: в kubeconfig

# --- 1. Поднять станцию одной командой (ждёт k3s, распечатывает Vault, чинит поды) ---
./scripts/ops/station_up.sh

# --- 2. Проверить, что распечатался Vault ---
kubectl -n vault exec rescue-ai-vault-0 -- vault status | grep Sealed   # Sealed  false

# --- 3. Проверить ВСЕ поды во всех namespace ---
kubectl get pods -A | grep -E 'rescue-ai|monitoring|vault'
#   ждём Running и READY n/n у всех. minio-bucket-init — это hook-Job: он
#   отрабатывает и ИСЧЕЗАЕТ (Completed/удалён), его в списке быть не должно.
#   Если прикладной под висит в Init без vault-agent-init — протух cert
#   инжектора; station_up.sh шаг 3/4 это уже лечит (пересоздаёт инжектор).

# --- 4. Функциональные проверки ---
API=$(kubectl -n rescue-ai get pod -l app.kubernetes.io/name=rescue-ai-api --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
kubectl -n rescue-ai exec $API -c api -- python -c "import urllib.request;print('ready:',urllib.request.urlopen('http://localhost:8000/ready').read().decode())"
kubectl -n rescue-ai exec $API -c api -- python -c "import urllib.request;print('rpi:',urllib.request.urlopen('http://localhost:8000/rpi/status').read().decode())"  # connected:true если дрон в сети

# Prometheus: все таргеты up?
PROM=$(kubectl -n monitoring get pod -o name | grep prometheus | cut -d/ -f2)
kubectl -n monitoring exec $PROM -- sh -c 'wget -qO- "http://localhost:9090/api/v1/query?query=up"' | python3 -c "import sys,json;[print(r['metric'].get('job'),'->','UP' if r['value'][1]=='1' else 'DOWN') for r in json.load(sys.stdin)['data']['result']]"

# --- 5. (опционально) открыть UI с Mac через port-forward ---
kubectl -n rescue-ai port-forward svc/rescue-ai-rescue-ai-api 8000:8000   # оператор: http://localhost:8000/pilot
kubectl -n monitoring port-forward svc/rescue-ai-observability-grafana 3000:3000   # Grafana: http://localhost:3000 (admin / пароль из Vault)