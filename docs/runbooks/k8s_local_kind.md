# Runbook: локальный K8s через kind / k3d + Helm

**Контекст:** ADR-0008. Поднимает rescue-ai в локальном Kubernetes
кластере с помощью `kind` или `k3d` + Helm. Подходит для разработки
и демонстрации на защите. Все три профиля (cloud / offline / hybrid)
проверяются одной командой `helm install -f values/<profile>.yaml`.

## Какой дистрибутив выбрать

| Инструмент | Что под капотом | Когда использовать |
|---|---|---|
| **`kind`** | Vanilla Kubernetes в Docker | Дефолт для Windows-разработки. Ближе к managed K8s в облаке. |
| **`k3d`** | k3s в Docker | Локальное приближение к **полевому деплою** на станцию (там тот же k3s). На защите можно показать «тот же бинарник, что в поле». |

Helm-чарты и values-файлы **идентичны** для обоих вариантов — выбираешь
дистрибутив исходя из того, что хочешь продемонстрировать.

## Предусловия

| Инструмент | Версия | Что ставить (Windows) |
|---|---|---|
| Docker Desktop | 4.x+ | уже есть |
| `kind` | 0.20+ | `choco install kind` или скачать [exe](https://kind.sigs.k8s.io/) |
| `k3d` (альтернатива) | 5.6+ | `choco install k3d` или [инструкция](https://k3d.io/) |
| `kubectl` | 1.29+ | `choco install kubernetes-cli` |
| `helm` | 3.13+ | `choco install kubernetes-helm` |
| `vault` (CLI) | 1.15+ | `choco install vault` (нужен только для bootstrap-скрипта) |

Проверка:
```powershell
docker version
kind version
kubectl version --client
helm version
```

## Шаг 1. Создать локальный кластер

Выбери **один** из двух вариантов — дальнейшие шаги идентичны.

### Вариант A: kind (по умолчанию)

```powershell
kind create cluster --name rescue-ai
kubectl config use-context kind-rescue-ai
kubectl get nodes
```

Должен появиться один control-plane node `rescue-ai-control-plane`.

### Вариант B: k3d (локальное приближение к полевому k3s)

```powershell
k3d cluster create rescue-ai
kubectl config use-context k3d-rescue-ai
kubectl get nodes
```

Должен появиться один node `k3d-rescue-ai-server-0`. По умолчанию
k3d поднимает в кластере встроенный Traefik как Ingress controller —
ровно как и k3s на станции.

## Шаг 2. Загрузить локальный образ rescue-ai в кластер

И kind, и k3d держат свой image store, **отдельный от Docker Desktop**.
Без этого `imagePullPolicy: IfNotPresent` упадёт с `ErrImageNeverPull`.

### Для kind

```powershell
kind load docker-image rescue-ai/online:local --name rescue-ai
```

### Для k3d

```powershell
k3d image import rescue-ai/online:local -c rescue-ai
```

## Шаг 3. Обновить зависимости umbrella-чарта

```powershell
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo add hashicorp https://helm.releases.hashicorp.com
helm repo update
helm dependency update infra/k8s/charts/rescue-ai
```

## Шаг 4. Профиль `dev` — самый быстрый smoke

```powershell
kubectl create namespace rescue-ai
kubectl -n rescue-ai create configmap pg-init --from-file=infra/postgres/init/
helm install rescue-ai infra/k8s/charts/rescue-ai -n rescue-ai -f infra/k8s/values/dev.yaml
kubectl -n rescue-ai get pods -w
```

Pod должен дойти до `Running 1/1`. Проверка:
```powershell
kubectl -n rescue-ai port-forward svc/rescue-ai-rescue-ai-api 8000:8000
curl http://localhost:8000/health
```

В отдельном терминале:
```powershell
kubectl -n rescue-ai logs -l app.kubernetes.io/name=rescue-ai-api -f
```

## Шаг 5. Профиль `offline` — Postgres + MinIO + Vault в кластере

Сначала ConfigMap с init-скриптами Postgres (нужен offline.yaml):
```powershell
kubectl -n rescue-ai create configmap pg-init `
  --from-file=infra/postgres/init/000-app-schema-bootstrap.sql `
  --from-file=infra/postgres/init/010-app-schema.sql `
  --from-file=infra/postgres/init/011-auto-mode-schema.sql `
  --from-file=infra/postgres/init/020-replication-outbox.sql
```

Сертификаты mTLS (если уже сгенерированы по runbook'у `rpi_mtls_setup.md`):
```powershell
kubectl -n rescue-ai create secret generic rpi-mtls `
  --from-file=ca.crt=scripts/security/out/station-root-ca.crt `
  --from-file=client.crt=scripts/security/out/gcs-client.crt `
  --from-file=client.key=scripts/security/out/gcs-client.key
```

Развёртывание:
```powershell
helm upgrade --install rescue-ai infra/k8s/charts/rescue-ai -n rescue-ai -f infra/k8s/values/offline.yaml
```

Подождать, пока поднимутся: `rescue-ai-postgresql-0`, `rescue-ai-minio-0`,
`rescue-ai-vault-0`, `rescue-ai-vault-agent-injector-*`,
`rescue-ai-rescue-ai-api-*`.

## Шаг 6. Bootstrap Vault (offline / hybrid)

```powershell
kubectl -n rescue-ai port-forward svc/rescue-ai-vault 8200:8200
```

В другом терминале:
```powershell
$env:VAULT_ADDR="http://localhost:8200"
$env:VAULT_TOKEN="root-dev-token"
$env:NAMESPACE="rescue-ai"
$env:VAULT_NAMESPACE="rescue-ai"
$env:VAULT_SERVICE_ACCOUNT="rescue-ai-vault"
bash scripts/security/vault_bootstrap.sh
```

Если запускаете этот шаг из Git Bash, используйте bash-синтаксис:
```bash
export VAULT_ADDR="http://localhost:8200"
export VAULT_TOKEN="root-dev-token"
export NAMESPACE="rescue-ai"
export VAULT_NAMESPACE="rescue-ai"
export VAULT_SERVICE_ACCOUNT="rescue-ai-vault"
bash scripts/security/vault_bootstrap.sh
```

Скрипт включит KV v2, k8s auth, загрузит политики и засеет
секреты в `secret/rescue-ai/api` + `secret/rescue-ai/sync-worker`.

После этого нужно перезапустить API-под, чтобы Vault Agent
Injector дописал секреты:
```powershell
kubectl -n rescue-ai rollout restart deployment rescue-ai-rescue-ai-api
kubectl -n rescue-ai logs -l app.kubernetes.io/name=rescue-ai-api -c vault-agent-init
```

В логах vault-agent-init должно быть `success` и появится файл
`/vault/secrets/app.env` внутри пода.

## Шаг 7. Профиль `hybrid` — добавить sync-worker

```powershell
helm upgrade rescue-ai infra/k8s/charts/rescue-ai -n rescue-ai -f infra/k8s/values/hybrid.yaml
kubectl -n rescue-ai get pods
```

Появится `rescue-ai-rescue-ai-sync-worker-*`. Логи:
```powershell
kubectl -n rescue-ai logs -l app.kubernetes.io/component=sync-worker -f
```

## Очистка

```powershell
helm uninstall rescue-ai -n rescue-ai
kubectl delete namespace rescue-ai
```

Удаление кластера зависит от выбранного дистрибутива:

```powershell
# Если использовал kind:
kind delete cluster --name rescue-ai

# Если использовал k3d:
k3d cluster delete rescue-ai
```

## Демо-сценарий на защите (5 минут)

1. `kind create cluster --name rescue-ai-demo` (или `k3d cluster create
   rescue-ai-demo`) — K8s запускается одной командой. Если выбрал
   k3d — упомянуть, что **тот же бинарник** (k3s) ставится на полевую
   станцию командой `curl -sfL https://get.k3s.io | sh -`.
2. `helm install rescue-ai ... -f values/dev.yaml` — простой случай.
3. `kubectl get pods` — pod `Running`.
4. `helm upgrade rescue-ai ... -f values/offline.yaml` — тот же
   чарт, другой values: подтягиваются Postgres + MinIO + Vault.
5. Vault UI на `localhost:8200` (`root-dev-token`) — показать KV
   secrets и policies.
6. `kubectl describe pod ...api... | grep vault.hashicorp.com` —
   видны annotation'ы Vault Agent Injector.
7. `kubectl exec ...api... -- cat /vault/secrets/app.env` —
   показать, что секреты пришли как файл, а не env-vars.

## Связь с полевым деплоем (k3s)

На полевой станции (Linux) ставится **тот же** k3s, что k3d
эмулирует локально:

```bash
# на станции — Linux, не Windows:
curl -sfL https://get.k3s.io | sh -
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
helm install rescue-ai infra/k8s/charts/rescue-ai \
    -n rescue-ai --create-namespace \
    -f infra/k8s/values/offline.yaml
```

Helm-чарты и values-файлы — без изменений. Это и есть инвариант
ADR-0008: один deployment pipeline для kind/k3d/k3s/managed K8s.

## Связанные документы

- [ADR-0008 — Kubernetes и Vault](../adr/ADR-0008-kubernetes-and-secrets.md)
- [ADR-0007 — Offline / Hybrid профили](../adr/ADR-0007-autonomous-deployment-and-offline-sync.md)
- [Runbook mTLS до RPi](rpi_mtls_setup.md)
- [Helm-чарты](../../infra/k8s/charts/)
- [Values для профилей](../../infra/k8s/values/)
