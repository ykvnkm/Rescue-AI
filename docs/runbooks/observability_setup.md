# Observability setup — Prometheus + Alertmanager + Grafana как Helm chart

## Кратко

Контур наблюдаемости развёрнут как отдельный helm release
`rescue-ai-observability` в **отдельном namespace `monitoring`** (CIS
K8s Benchmark §5.7.1, учебная норма). Это третий umbrella в системе:

| umbrella | namespace | что | где |
|---|---|---|---|
| `rescue-ai` | `rescue-ai` | online контур (api, detection, nav-engine, sync-worker, локальные Postgres/MinIO, Vault Agent) | offline + cloud |
| `rescue-batch` | `rescue-batch` | Airflow + batch-exporter | cloud-only |
| `rescue-ai-observability` | `monitoring` | Prometheus + Alertmanager + Grafana | offline + cloud |

Установка каждого — отдельный `helm install`. Vault разворачивается ещё
раньше отдельным release-ом, см. [vault_setup.md](vault_setup.md).

## Структура chart-а

```
infra/k8s/charts/rescue-ai-observability/
├── Chart.yaml
├── values.yaml
├── files/                      ← общий источник конфигов для compose И k8s
│   ├── prometheus/
│   │   ├── prometheus.yml      (compose-вариант, hardcoded на docker DNS)
│   │   ├── rules-station.yml   (alert rules станции)
│   │   └── rules-central.yml   (alert rules центра)
│   ├── alertmanager/
│   │   ├── alertmanager.local.yml  (offline — receiver `local-dev`)
│   │   └── alertmanager.yml        (cloud — SMTP/Slack receiver)
│   └── grafana/
│       ├── provisioning/{datasources,dashboards}/*.yaml
│       └── dashboards/{station,central}/*.json
└── templates/
    ├── _helpers.tpl
    ├── namespace.yaml          (опционально, если createNamespace=true)
    ├── prometheus-configmap.yaml   (k8s-вариант prometheus.yml с FQDN + rules)
    ├── prometheus-deployment.yaml  (+ Service + PVC + SA)
    ├── alertmanager-configmap.yaml
    ├── alertmanager-deployment.yaml (+ Service + PVC)
    ├── grafana-configmap.yaml      (3 CM: datasources / provisioning / dashboards)
    ├── grafana-secret.yaml         (inline admin для dev)
    ├── grafana-deployment.yaml     (+ Service + PVC + Ingress)
    └── networkpolicy.yaml          (default-deny + 7 allow-правил)
```

Profile values:
- `infra/k8s/values/observability-offline.yaml` — станция: маленький Prometheus, local-режим Alertmanager, Grafana без Ingress.
- `infra/k8s/values/observability-cloud.yaml` — центр: большой Prometheus (30 дней retention, 50 GiB PVC), прод-Alertmanager (SMTP/Slack), Grafana с Ingress + cert-manager TLS.

## Установка (offline / k3s на станции)

После того как rescue-ai-vault и rescue-ai установлены (см.
[vault_setup.md](vault_setup.md), [k3s_field_deploy.md](k3s_field_deploy.md)).
Сначала записываем admin-учётку Grafana в Vault (она поднимается через
Vault Agent в pod-е, отдельного k8s Secret больше нет):

```bash
kubectl -n vault port-forward svc/rescue-ai-vault 8200:8200 &
VAULT_ADDR=http://127.0.0.1:8200 \
VAULT_TOKEN=<root-or-operator-token> \
NAMESPACE=rescue-ai \
VAULT_NAMESPACE=vault \
ENABLE_LOCAL_INFRA=false \
ENABLE_GRAFANA=true \
GRAFANA_ADMIN_USER=admin \
GRAFANA_ADMIN_PASSWORD='<station-grafana-password>' \
DB_DSN='postgresql://...' \
ARTIFACTS_S3_ACCESS_KEY_ID='...' \
ARTIFACTS_S3_SECRET_ACCESS_KEY='...' \
    ./scripts/security/vault_bootstrap.sh
kill %1

# Теперь сам observability
kubectl create namespace monitoring

helm install rescue-ai-observability \
    infra/k8s/charts/rescue-ai-observability \
    -n monitoring \
    -f infra/k8s/values/observability-offline.yaml

kubectl -n monitoring rollout status deployment/rescue-ai-observability-prometheus
kubectl -n monitoring rollout status deployment/rescue-ai-observability-alertmanager
kubectl -n monitoring rollout status deployment/rescue-ai-observability-grafana
```

Открыть UI на macOS-операторе:

```bash
kubectl -n monitoring port-forward svc/rescue-ai-observability-grafana 3000:3000 &
open http://localhost:3000   # login: admin / <станичный пароль>
```

> **Alertmanager в offline** работает в local-режиме (`alertmanager.local.yml`,
> receiver `local-dev`) — алерты видны только в UI Alertmanager на
> `port-forward 9093`. SMTP не используется, Vault Agent в Alertmanager
> на станции не подключён.

## Установка (cloud)

В cloud Vault Agent рендерит **и** Grafana admin, **и** Alertmanager
SMTP credentials. Сначала bootstrap расширяется обоими флагами:

```bash
kubectl -n vault port-forward svc/rescue-ai-vault 8200:8200 &

VAULT_ADDR=http://127.0.0.1:8200 \
VAULT_TOKEN=<root-token> \
NAMESPACE=rescue-ai \
VAULT_NAMESPACE=vault \
ENABLE_LOCAL_INFRA=false \
ENABLE_BATCH_EXPORTER=true \
ENABLE_AIRFLOW=true \
ENABLE_GRAFANA=true \
ENABLE_ALERTMANAGER=true \
DB_DSN='postgresql://rescue@rescue-app.mdb.yandexcloud.net:6432/rescue_ai' \
ARTIFACTS_S3_ACCESS_KEY_ID='<yc-key>' \
ARTIFACTS_S3_SECRET_ACCESS_KEY='<yc-secret>' \
AIRFLOW_DB_CONN='postgresql://...' \
AIRFLOW_S3_CONN='{"conn_type":"aws",...}' \
GRAFANA_ADMIN_USER=admin \
GRAFANA_ADMIN_PASSWORD='<grafana-pass>' \
ALERT_SMTP_FROM='alerts@rescue-ai.example.com' \
ALERT_SMTP_SMARTHOST='smtp.yandex.ru:465' \
ALERT_SMTP_USERNAME='alerts@rescue-ai.example.com' \
ALERT_SMTP_PASSWORD='<smtp-password>' \
ALERT_WARNING_RECIPIENT='ops-warning@rescue-ai.example.com' \
ALERT_CRITICAL_RECIPIENT='on-call@rescue-ai.example.com' \
    ./scripts/security/vault_bootstrap.sh

kill %1

# Теперь сам observability — admin Grafana и SMTP-creds Alertmanager
# уже в Vault, никаких --set с паролями.
kubectl create namespace monitoring
helm install rescue-ai-observability \
    infra/k8s/charts/rescue-ai-observability \
    -n monitoring \
    -f infra/k8s/values/observability-cloud.yaml

kubectl -n monitoring get pods
```

Grafana будет доступна по `https://grafana.rescue-ai.example.com`
через nginx + cert-manager TLS. Alertmanager в cloud при срабатывании
правила отправит email через указанный SMTP-релей на адрес warning или
critical (см. `routes` в `files/alertmanager/alertmanager.yml`).

## Что попадает в Prometheus

| Сервис | Где | Job в Prometheus | Контур |
|---|---|---|---|
| api `/metrics` | rescue-ai.svc:8000 | `rescue-ai-api` | station + central |
| detection `/metrics` | rescue-ai.svc:8002 | `rescue-ai-detection` | station + central |
| nav-engine `/metrics` | rescue-ai.svc:8001 | `rescue-ai-nav-engine` | station + central |
| batch-exporter `/metrics` | rescue-batch.svc:8003 | `rescue-ai-batch-exporter` | **central only** |
| self | localhost:9090 | `prometheus` | оба |

batch-exporter транслирует `batch_pipeline_metrics` + `drift_observations`
из Postgres в gauges `rescue_ai_batch_*` и `rescue_ai_drift_*` (см.
[drift_setup.md](drift_setup.md)).

## Что увидит экзаменатор

```bash
# 1. Три umbrella, три namespace
kubectl get ns | grep -E 'rescue-ai|rescue-batch|monitoring'
helm list -A | grep -E 'rescue-ai|rescue-batch'

# 2. Prometheus реально скрейпит наши сервисы
kubectl -n monitoring exec deploy/rescue-ai-observability-prometheus -- \
    wget -qO- http://localhost:9090/api/v1/targets | head -50

# 3. Alert rules загружены (только нужный контур)
kubectl -n monitoring exec deploy/rescue-ai-observability-prometheus -- \
    wget -qO- http://localhost:9090/api/v1/rules | head -50

# 4. NetworkPolicy default-deny в namespace monitoring
kubectl -n monitoring get networkpolicy
# monitoring-default-deny
# monitoring-allow-dns
# monitoring-prom-scrape-app
# monitoring-prom-scrape-batch   (только central)
# monitoring-prom-to-alertmanager
# monitoring-grafana-ingress
# monitoring-grafana-to-prom
```

## Связанные документы

- [Vault setup](vault_setup.md) — Vault ставится первым
- [k3s field deploy](k3s_field_deploy.md) — установка online контура
- [batch_namespace](batch_namespace.md) — batch контур
- [drift_setup](drift_setup.md) — что попадает в дашборд «Data drift»
- [incident_response](incident_response.md) — диагностика
