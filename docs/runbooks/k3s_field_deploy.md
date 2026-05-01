# Runbook: полевой деплой Rescue-AI на k3s

**Контекст:** ADR-0007 + ADR-0008 §2. Это инструкция для операционного
сценария **«поле»** — однонодная Linux-машина (станция оператора,
ноут, mini-PC), без интернета или с эпизодической связью. Установить
k3s, развернуть rescue-ai по offline (или hybrid) values-файлу,
сгенерировать сертификаты, засеять Vault.

Для **локальной разработки на Windows** используй
[k8s_local_kind.md](k8s_local_kind.md) — там та же топология, но в
kind/k3d.

## Предусловия

- Linux (Ubuntu 22.04+ / Debian 12 / Fedora 39+) на станции.
- Минимум 4 ГБ RAM, 20 ГБ свободного диска (под Postgres/MinIO/модели).
- Образ Rescue-AI (`rescue-ai/online:<tag>`), доставленный на станцию
  одним из способов (см. шаг 2 ниже).
- Файлы репо `infra/k8s/` и `scripts/security/` — либо из git, либо
  скопированные на станцию архивом.

## Шаг 1. Установить k3s

```bash
curl -sfL https://get.k3s.io | sh -

# kubectl-конфиг для текущего пользователя
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
sudo chown "$USER:$USER" ~/.kube/config
chmod 600 ~/.kube/config

kubectl get nodes
```

Должна появиться одна нода в статусе `Ready`. k3s сам развернёт
встроенный Traefik как ingress controller.

## Шаг 2. Доставить образ rescue-ai в кластер

В поле нет registry, поэтому образ либо собирается на месте, либо
импортируется из tar:

### Вариант A — собран на станции
```bash
docker build -t rescue-ai/online:local -f Dockerfile .
# k3s использует containerd, нужно импортировать в его image store:
docker save rescue-ai/online:local -o /tmp/rescue-ai.tar
sudo k3s ctr images import /tmp/rescue-ai.tar
```

### Вариант B — привезли tar с собой (типичный полевой сценарий)
```bash
# собрано заранее на ноуте: docker save rescue-ai/online:local -o rescue-ai.tar
sudo k3s ctr images import rescue-ai.tar
sudo k3s ctr images list | grep rescue-ai
```

Проверка:
```bash
sudo k3s ctr images list -q | grep rescue-ai/online
```

## Шаг 3. Сгенерировать сертификаты mTLS

Один раз на станции, по [`rpi_mtls_setup.md`](rpi_mtls_setup.md):

```bash
./scripts/security/gen_ca.sh
./scripts/security/gen_rpi_cert.sh
./scripts/security/gen_client_cert.sh
ls scripts/security/out/
```

`rpi-server.{crt,key}` нужно отдельно перенести на Pi
(см. runbook про mTLS), здесь они нужны только для контекста.

## Шаг 4. Создать namespace и базовые ресурсы

```bash
kubectl create namespace rescue-ai

# init-скрипты Postgres (включая 000-app-schema-bootstrap.sql,
# который создаёт схему `app` — иначе репозитории читают пустой public).
kubectl -n rescue-ai create configmap pg-init \
    --from-file=infra/postgres/init/

# материал mTLS — клиентская сторона для api
kubectl -n rescue-ai create secret generic rpi-mtls \
    --from-file=ca.crt=scripts/security/out/station-root-ca.crt \
    --from-file=client.crt=scripts/security/out/gcs-client.crt \
    --from-file=client.key=scripts/security/out/gcs-client.key
```

## Шаг 5. Подтянуть зависимости umbrella-чарта

Если на станции есть интернет (хотя бы один раз для скачивания
chart tarball'ов):
```bash
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo add hashicorp https://helm.releases.hashicorp.com
helm repo update
helm dependency update infra/k8s/charts/rescue-ai
```

Если интернета нет — заранее с другого ноута выполни ту же команду,
скопируй директорию `infra/k8s/charts/rescue-ai/charts/` целиком
(там уже tgz зависимостей) на станцию.

## Шаг 6. Развернуть offline-профиль

```bash
helm install rescue-ai infra/k8s/charts/rescue-ai \
    -n rescue-ai \
    -f infra/k8s/values/offline.yaml
```

Дождаться pods ready:
```bash
kubectl -n rescue-ai get pods -w
```

Должны подняться: `rescue-ai-postgresql-0`, `rescue-ai-minio-0`,
`rescue-ai-vault-0`, `rescue-ai-vault-agent-injector-*`,
`rescue-ai-rescue-ai-api-*`.

## Шаг 7. Bootstrap Vault

```bash
kubectl -n rescue-ai port-forward svc/rescue-ai-vault 8200:8200 &
export VAULT_ADDR="http://localhost:8200"
export VAULT_TOKEN="root-dev-token"
export NAMESPACE="rescue-ai"
export VAULT_NAMESPACE="rescue-ai"
export VAULT_SERVICE_ACCOUNT="rescue-ai-vault"
bash scripts/security/vault_bootstrap.sh
```

Скрипт включит KV v2, k8s auth, политики и засеет секреты.

После этого перезапустить api, чтобы Vault Agent Injector положил
файл с секретами в `/vault/secrets/app.env`:
```bash
kubectl -n rescue-ai rollout restart deployment rescue-ai-rescue-ai-api
kubectl -n rescue-ai logs \
    -l app.kubernetes.io/name=rescue-ai-api -c vault-agent-init --tail=20
```

## Шаг 8. Проверка

```bash
kubectl -n rescue-ai port-forward svc/rescue-ai-rescue-ai-api 8000:8000 &
curl http://localhost:8000/health
```

Должен ответить `{"status":"ok"}`. UI открывается на `http://<station-ip>:8000`
(если хочешь публиковать через Traefik — отредактируй `ingress.enabled`
в `offline.yaml` и подними DNS-запись).

## Переключение в hybrid (когда есть интернет)

Когда у станции эпизодически появляется интернет — переключить
профиль одной командой:
```bash
helm upgrade rescue-ai infra/k8s/charts/rescue-ai \
    -n rescue-ai \
    -f infra/k8s/values/hybrid.yaml
```

Это:
- оставит Postgres + MinIO как локальные authoritative-стора;
- запустит **дополнительный** под `rescue-ai-rescue-ai-sync-worker`,
  который дрейнит `replication_outbox` в remote Supabase + Yandex S3.

Logs:
```bash
kubectl -n rescue-ai logs \
    -l app.kubernetes.io/component=sync-worker -f
```

## Обновление образа

Когда привозишь новый билд (tar):
```bash
sudo k3s ctr images import rescue-ai-online-<tag>.tar
helm upgrade rescue-ai infra/k8s/charts/rescue-ai \
    -n rescue-ai \
    -f infra/k8s/values/offline.yaml \
    --set rescue-ai-api.image.tag=<tag>
```

k3s автоматически перезапустит api-pod с новым тегом.

## Откат

```bash
helm history rescue-ai -n rescue-ai
helm rollback rescue-ai <REVISION> -n rescue-ai
```

## Полная очистка

```bash
helm uninstall rescue-ai -n rescue-ai
kubectl delete namespace rescue-ai
sudo /usr/local/bin/k3s-uninstall.sh   # если хочешь снести и k3s
```

## Чек-лист «всё работает в поле»

- [ ] `kubectl get nodes` → 1 нода `Ready`.
- [ ] `kubectl -n rescue-ai get pods` → все `Running`/`Completed`.
- [ ] `kubectl -n rescue-ai exec rescue-ai-postgresql-0 -- psql -U rescue -d rescue_ai -c "SELECT count(*) FROM app.replication_outbox"` → выполняется.
- [ ] `curl http://localhost:8000/health` → 200.
- [ ] `kubectl -n rescue-ai exec ...api... -- cat /vault/secrets/app.env` → файл есть, в нём DSN/S3-креды.
- [ ] Запущена тестовая миссия через UI → точки в `app.auto_trajectory_points`.
- [ ] (только hybrid) `kubectl -n rescue-ai logs ...sync-worker...` показывает успешные drain-итерации.

## Связанные документы

- [ADR-0007 — Offline / Hybrid профили](../adr/ADR-0007-autonomous-deployment-and-offline-sync.md)
- [ADR-0008 — Kubernetes и Vault](../adr/ADR-0008-kubernetes-and-secrets.md)
- [Локальный K8s через kind/k3d](k8s_local_kind.md)
- [Настройка mTLS на Raspberry Pi](rpi_mtls_setup.md)
