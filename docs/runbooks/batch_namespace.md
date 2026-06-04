# Runbook — batch-контур в namespace `rescue-batch`

## Зачем отдельный namespace

ADR-0008 §4. Прикладные сервисы (api, detection, nav-engine,
sync-worker) живут в `rescue-ai`. Batch-контур (Airflow + наш
batch-exporter + ephemeral batch-worker pods) — в `rescue-batch`.
Разделение даёт:

- **Ресурсная изоляция.** `ResourceQuota` ограничивает batch
  суммарно 8 CPU / 16 GiB requests, 16 CPU / 32 GiB limits на весь
  namespace. ML-стадия с torch не съест online-обработку миссий.
- **Сетевая изоляция.** `NetworkPolicy` default-deny + явные правила:
  разрешён DNS, egress к Vault, к managed Postgres / S3 (порты 5432,
  6432, 443, 22 для git-sync). Ingress только от namespace
  `monitoring`. Доступа в `rescue-ai` нет.
- **Независимый lifecycle.** `helm upgrade rescue-ai` не трогает
  Airflow. `helm upgrade rescue-batch` не трогает api.
- **Минимальные RBAC.** Vault role `rescue-ai-airflow` биндится к
  ServiceAccount-ам только из `rescue-batch`. Доступ к Vault KV
  путям `secret/airflow/*` — только Airflow-подам.

Batch-контур разворачивается **только в cloud**. На наземной станции
(offline) ежедневный batch-пересчёт качества модели не имеет смысла —
он осмыслен только над аккумулированным корпусом миссий со всех
станций (см. ML design doc §3.5).

## Что внутри umbrella `rescue-batch`

```
infra/k8s/charts/rescue-batch/
├── Chart.yaml                  # deps: apache-airflow + наш batch-exporter
├── values.yaml                 # дефолты с enabled=false
└── templates/
    ├── _helpers.tpl
    ├── namespace.yaml          # optional, если createNamespace=true
    ├── resourcequota.yaml      # 8/16 CPU, 16/32 GiB
    ├── limitrange.yaml         # дефолты для подов без resources
    ├── networkpolicy.yaml      # default-deny + DNS + Vault + external + monitoring ingress
    └── worker-serviceaccount.yaml   # SA rescue-batch-worker для KubernetesPodOperator pods
```

Subcharts: `airflow` (stock apache-airflow Helm chart) и наш
`rescue-ai-batch-exporter` (file-локально через `repository: file://../`).

## Последовательность установки

В cloud-кластере (предполагается, что rescue-ai-vault уже установлен
в namespace `vault` и пройден `vault operator init + unseal`):

```bash
# 1. Namespace + GHCR pull secret + Airflow meta-db secret
kubectl create namespace rescue-batch --dry-run=client -o yaml \
    | kubectl apply -f -

kubectl -n rescue-batch create secret docker-registry ghcr-pull \
    --docker-server=ghcr.io \
    --docker-username="$GHCR_USERNAME" \
    --docker-password="$GHCR_TOKEN" \
    --dry-run=client -o yaml | kubectl apply -f -

# Airflow metadata DB (chicken-and-egg секрет, см. vault_setup.md)
kubectl -n rescue-batch create secret generic rescue-airflow-meta-db \
    --from-literal=connection="$AIRFLOW_DB_URI" \
    --dry-run=client -o yaml | kubectl apply -f -

# 2. Vault bootstrap (один раз, расширяем rescue-ai-vault новыми ролями)
kubectl -n vault port-forward svc/rescue-ai-vault 8200:8200 &
VAULT_ADDR=http://127.0.0.1:8200 \
VAULT_TOKEN="$VAULT_ROOT_TOKEN" \
NAMESPACE=rescue-ai \
BATCH_NAMESPACE=rescue-batch \
VAULT_NAMESPACE=vault \
ENABLE_BATCH_EXPORTER=true \
ENABLE_AIRFLOW=true \
AIRFLOW_DB_CONN="postgresql://..." \
AIRFLOW_S3_CONN='{"conn_type":"aws","login":"...","password":"...","extra":{...}}' \
DB_DSN="$DB_DSN" \
ARTIFACTS_S3_ACCESS_KEY_ID="$YC_KEY" \
ARTIFACTS_S3_SECRET_ACCESS_KEY="$YC_SECRET" \
    ./scripts/security/vault_bootstrap.sh
kill %1

# 3. Helm dependencies + установка
helm dependency update infra/k8s/charts/rescue-batch

helm upgrade --install rescue-batch infra/k8s/charts/rescue-batch \
    -n rescue-batch \
    -f infra/k8s/values/rescue-batch-cloud.yaml \
    --set rescue-ai-batch-exporter.image.repository=ghcr.io/ykvnkm/rescue-ai-batch-exporter \
    --set rescue-ai-batch-exporter.image.tag=latest \
    --set "airflow.env[0].value=ghcr.io/ykvnkm/rescue-ai-batch-worker:latest" \
    --wait --timeout 15m

# 4. Проверка
kubectl -n rescue-batch get pods
kubectl -n rescue-batch get networkpolicy
kubectl -n rescue-batch get resourcequota
kubectl -n rescue-batch get serviceaccount

# Должен быть SA rescue-batch-worker — это и есть identity для
# ephemeral KubernetesPodOperator-подов ML-стадий.
```

## Как Airflow поднимает ML-стадии

1. Scheduler читает DAG `rescue_batch_pipeline` (через git-sync).
2. На каждой таске (`prepare_dataset` / `evaluate_model` /
   `publish_metrics` / `compute_drift`) `KubernetesPodOperator` создаёт
   pod из образа `rescue-ai-batch-worker` в namespace `rescue-batch`,
   под ServiceAccount-ом `rescue-batch-worker`.
3. Этот pod наследует `NetworkPolicy` namespace'а: видит Postgres
   (port 5432), S3 (443), Vault (8200), не видит ничего в `rescue-ai`.
4. Секреты (`DB_DSN`, `ARTIFACTS_S3_*`) пробрасываются Airflow через
   `env` оператора — Airflow сам забирает их из Vault через Secrets
   Backend и подставляет.
5. После завершения таски pod удаляется (это и есть «ephemeral
   KubernetesExecutor pattern»).

## Что увидит экзаменатор

```bash
# 1. Online и batch — разные namespace
kubectl get pods -n rescue-ai      # api/detection/nav-engine/sync-worker
kubectl get pods -n rescue-batch   # airflow + batch-exporter

# 2. NetworkPolicy default-deny на batch namespace
kubectl get networkpolicy -n rescue-batch
# rescue-batch-default-deny
# rescue-batch-allow-dns
# rescue-batch-allow-vault
# rescue-batch-allow-external
# rescue-batch-allow-monitoring-ingress

# 3. ResourceQuota
kubectl describe resourcequota -n rescue-batch
# requests.cpu  0/8
# limits.cpu    0/16  и т.д.

# 4. Airflow pod не может ходить в rescue-ai
kubectl -n rescue-batch exec deploy/rescue-batch-airflow-scheduler -- \
    nc -zv rescue-ai-rescue-ai-api.rescue-ai.svc.cluster.local 8000
# Connection refused (заблокировано NetworkPolicy).

# 5. Vault role для airflow привязан строго к rescue-batch
kubectl -n vault exec rescue-ai-vault-0 -- \
    vault read auth/kubernetes/role/rescue-ai-airflow
# bound_service_account_namespaces: [rescue-batch]
```

## Связанные документы

- [ADR-0008 §4](../adr/ADR-0008-kubernetes-and-secrets.md) — обоснование
- [Vault setup](vault_setup.md) — bootstrap policies/roles
- [k3s field deploy](k3s_field_deploy.md) — offline (batch не разворачивает)
- [Incident response](incident_response.md) §11 — Airflow scheduler не видит DAG-и
