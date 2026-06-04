# Runbook: полевой prod-деплой Rescue-AI на k3s

**Контекст:** ADR-0007 + ADR-0008. Это инструкция для строгого
offline/hybrid prod-сценария: однонодная Linux-станция оператора,
локальные Postgres/MinIO, Vault как единственный source of truth для
долгоживущих секретов, k3s как runtime.

Для локальной разработки k3s не используется. Dev-стенд запускается
через docker-compose из корня репозитория:

```bash
docker compose up --build
```

## Целевая модель

В offline-prod **не создаём Kubernetes Secret для Postgres, MinIO,
`DB_DSN`, S3-ключей или mTLS-ключей**. Последовательность такая:

1. Сначала ставится отдельный Helm-релиз Vault.
2. Оператор делает `vault operator init` и `unseal`.
3. `vault_bootstrap.sh` заводит в Vault infra/app/mTLS секреты и
   Kubernetes auth roles.
4. Umbrella-чарт `rescue-ai` поднимает локальные Postgres/MinIO и
   приложения. Все они получают credentials через Vault Agent Injector.

`Kubernetes Secret` остаётся допустимым только для bootstrap-вещей,
без которых сам Kubernetes не может работать, например `imagePullSecret`
для приватного registry или TLS Secret, который создаёт cert-manager
для ingress. В базовом offline runbook ниже такие Secrets не нужны.

## Что разворачивается на станции

`infra/k8s/values/offline.yaml` включает:

- `rescue-ai-api`
- `rescue-ai-detection`
- `rescue-ai-nav-engine`
- `rescue-ai-sync-worker`
- `localPostgresql` — локальный StatefulSet PostgreSQL, пароль из Vault
- `localMinio` — локальный StatefulSet MinIO, root credentials из Vault

Отдельно до umbrella-чарта ставится:

- `rescue-ai-vault` — HashiCorp Vault release в namespace `vault`
  с values из `infra/k8s/vault/values-offline.yaml`

Отдельного `hybrid.yaml` нет. Если в Vault заполнены
`DEPLOYMENT_REMOTE_*`, sync-worker дренирует `replication_outbox` в
центральный контур при появлении связи. Если они пустые, станция
работает полностью offline.

## Предусловия

- Linux-станция: Ubuntu 22.04+ / Debian 12 / Fedora 39+.
- Минимум 4 ГБ RAM и 20 ГБ диска; для MinIO лучше закладывать больше.
- Установлены `kubectl`, `helm`, `vault` CLI и Docker/Podman.
- На станции есть репозиторий или архив с каталогами `infra/k8s/`,
  `infra/postgres/init/` и `scripts/security/`.
- На станции есть образы приложения:
  `rescue-ai-api:<tag>`, `rescue-ai-detection:<tag>`,
  `rescue-ai-nav-engine:<tag>`, `rescue-ai-sync-worker:<tag>`.
- Если registry недоступен, заранее привези tar-архивы всех runtime
  images: приложения, Vault, Vault Injector, `postgres:16-alpine`,
  `quay.io/minio/minio`, `quay.io/minio/mc`, `postgres:16-alpine`
  для init-контейнеров.

## 1. Установить k3s

```bash
curl -sfL https://get.k3s.io | sh -s - --secrets-encryption

mkdir -p ~/.kube
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
sudo chown "$USER:$USER" ~/.kube/config
chmod 600 ~/.kube/config

kubectl get nodes
kubectl get storageclass
```

Ожидаемый результат: одна нода `Ready`, default `StorageClass` обычно
`local-path`. `--secrets-encryption` оставляем включённым как базовую
prod-гигиену k3s, но не используем его как оправдание для хранения
прикладных или infra credentials в Kubernetes Secrets.

## 2. Подготовить Helm dependencies

Если на станции есть интернет:

```bash
helm repo add hashicorp https://helm.releases.hashicorp.com --force-update
helm repo add apache-airflow https://airflow.apache.org --force-update
helm repo update
helm dependency update infra/k8s/charts/rescue-ai
```

Эквивалентно:

```bash
make helm-deps
```

Если интернета нет, каталог `infra/k8s/charts/rescue-ai/charts/`
нужно подготовить заранее на машине с интернетом и перенести на
станцию. Не коммить `charts/*.tgz` в Git.

## 3. Доставить образы в k3s

Приложение собирается отдельными targets:

```bash
for svc in api detection nav-engine sync-worker; do
    docker build --target "$svc" -t "rescue-ai-$svc:<tag>" -f Dockerfile .
    docker save "rescue-ai-$svc:<tag>" -o "rescue-ai-$svc-<tag>.tar"
done
```

На станции:

```bash
for svc in api detection nav-engine sync-worker; do
    sudo k3s ctr images import "rescue-ai-$svc-<tag>.tar"
done

sudo k3s ctr images list -q | grep 'rescue-ai-'
```

Если нет доступа к внешнему registry, тем же способом импортируй
образы Vault, Vault Injector, Postgres, MinIO и MinIO Client.

## 4. Создать namespaces и init ConfigMap

```bash
# Namespace для security-инфраструктуры (Vault). Создаётся отдельно
# от namespace приложения — это каноническая изоляция (CIS K8s §5.7.1).
kubectl create namespace vault --dry-run=client -o yaml \
    | kubectl apply -f -

# Namespace для приложения.
kubectl create namespace rescue-ai --dry-run=client -o yaml \
    | kubectl apply -f -

kubectl -n rescue-ai create configmap pg-init \
    --from-file=infra/postgres/init/ \
    --dry-run=client -o yaml \
    | kubectl apply -f -
```

Здесь намеренно нет `kubectl create secret`: пароли Postgres/MinIO,
`DB_DSN`, S3 credentials и mTLS-ключи будут заведены в Vault.

## 5. Установить Vault отдельным релизом

```bash
helm upgrade --install rescue-ai-vault hashicorp/vault \
    -n vault \
    --version 0.30.0 \
    -f infra/k8s/vault/values-offline.yaml

kubectl -n vault get pods -l app.kubernetes.io/name=vault -w
```

Дождись pod `rescue-ai-vault-0`. Сначала он будет `Running`, но Vault
внутри будет sealed.

## 6. Init + unseal Vault

```bash
kubectl -n vault exec -it rescue-ai-vault-0 -- \
    vault operator init -key-shares=5 -key-threshold=3
```

Сохрани все 5 unseal keys и Initial Root Token вне кластера. Затем:

```bash
kubectl -n vault exec -it rescue-ai-vault-0 -- vault operator unseal <key-1>
kubectl -n vault exec -it rescue-ai-vault-0 -- vault operator unseal <key-2>
kubectl -n vault exec -it rescue-ai-vault-0 -- vault operator unseal <key-3>

kubectl -n vault exec -it rescue-ai-vault-0 -- vault status
```

Ожидаемый статус: `Initialized: true`, `Sealed: false`.

## 7. Сгенерировать mTLS-материал

Один раз на станции, по [`rpi_mtls_setup.md`](rpi_mtls_setup.md):

```bash
./scripts/security/gen_ca.sh
./scripts/security/gen_rpi_cert.sh
./scripts/security/gen_client_cert.sh
ls scripts/security/out/
```

`rpi-server.crt` и `rpi-server.key` перенеси на Raspberry Pi. Клиентские
файлы станции будут записаны в Vault, а не в Kubernetes Secret.

## 8. Bootstrap Vault

Оставь port-forward открытым на время bootstrap:

```bash
kubectl -n vault port-forward svc/rescue-ai-vault 8200:8200 &
```

Запусти bootstrap с реальными значениями:

```bash
VAULT_ADDR=http://127.0.0.1:8200 \
VAULT_TOKEN='<initial-root-token>' \
NAMESPACE=rescue-ai \
VAULT_NAMESPACE=vault \
VAULT_SERVICE_ACCOUNT=rescue-ai-vault \
ENABLE_LOCAL_INFRA=true \
ENABLE_PG_BACKUP=true \
ENABLE_BATCH_EXPORTER=false \
POSTGRES_USER=rescue \
POSTGRES_DB=rescue_ai \
POSTGRES_PASSWORD='<postgres-user-password>' \
MINIO_ROOT_USER='rescueadmin' \
MINIO_ROOT_PASSWORD='<minio-root-password>' \
DB_DSN='postgresql://rescue:<postgres-user-password>@rescue-ai-postgresql.rescue-ai.svc.cluster.local:5432/rescue_ai' \
ENABLE_RPI_MTLS=true \
MTLS_CA_CERT_FILE=scripts/security/out/station-root-ca.crt \
MTLS_CLIENT_CERT_FILE=scripts/security/out/gcs-client.crt \
MTLS_CLIENT_KEY_FILE=scripts/security/out/gcs-client.key \
DEPLOYMENT_REMOTE_DB_DSN='' \
DEPLOYMENT_REMOTE_S3_ACCESS_KEY_ID='' \
DEPLOYMENT_REMOTE_S3_SECRET_ACCESS_KEY='' \
    ./scripts/security/vault_bootstrap.sh
```

Для hybrid заполни `DEPLOYMENT_REMOTE_*` реальными значениями
центрального Postgres и S3. Локальные MinIO credentials автоматически
становятся `ARTIFACTS_S3_ACCESS_KEY_ID` /
`ARTIFACTS_S3_SECRET_ACCESS_KEY` для API, если не переопределить их
явно.

Остановить port-forward:

```bash
kill %1
```

## 9. Установить umbrella-чарт Rescue-AI

Для локальных тегов `local`:

```bash
helm upgrade --install rescue-ai infra/k8s/charts/rescue-ai \
    -n rescue-ai \
    -f infra/k8s/values/offline.yaml
```

Если теги другие:

```bash
helm upgrade --install rescue-ai infra/k8s/charts/rescue-ai \
    -n rescue-ai \
    -f infra/k8s/values/offline.yaml \
    --set rescue-ai-api.image.tag=<tag> \
    --set rescue-ai-detection.image.tag=<tag> \
    --set rescue-ai-nav-engine.image.tag=<tag> \
    --set rescue-ai-sync-worker.image.tag=<tag>
```

Дождаться готовности:

```bash
kubectl -n rescue-ai get pods -w
```

Что должно подняться:

- `rescue-ai-postgresql-0`
- `rescue-ai-minio-0`
- `rescue-ai-minio-bucket-init-*` на время создания bucket'а; после
  успеха hook-job удаляется Helm'ом
- `rescue-ai-rescue-ai-api-*`
- `rescue-ai-rescue-ai-detection-*`
- `rescue-ai-rescue-ai-nav-engine-*`
- `rescue-ai-rescue-ai-sync-worker-*`

## 10. Проверка

```bash
kubectl -n rescue-ai get pods

kubectl -n rescue-ai port-forward svc/rescue-ai-rescue-ai-api 8000:8000 &
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
```

Проверить, что в namespace нет наших credential Secrets:

```bash
kubectl -n rescue-ai get secret
```

Не должно быть `rescue-ai-postgresql-auth`, `rescue-ai-minio-auth`,
`rpi-mtls`, `rescue-ai-api-secret`, `rescue-ai-sync-worker-secret`.

Проверить Postgres:

```bash
kubectl -n rescue-ai exec rescue-ai-postgresql-0 -- \
    psql -U rescue -d rescue_ai \
    -c "SELECT count(*) FROM app.replication_outbox;"
```

Проверить Vault-rendered files:

```bash
kubectl -n rescue-ai exec deploy/rescue-ai-rescue-ai-api -c api -- \
    sh -c 'test -f /vault/secrets/app.env && test -f /vault/secrets/gcs-client.key'

kubectl -n rescue-ai exec statefulset/rescue-ai-postgresql -c postgresql -- \
    sh -c 'test -f /vault/secrets/postgres-password'

kubectl -n rescue-ai exec statefulset/rescue-ai-minio -c minio -- \
    sh -c 'test -f /vault/secrets/minio-root-password'
```

Проверить sync-worker:

```bash
kubectl -n rescue-ai logs \
    -l app.kubernetes.io/component=sync-worker \
    --tail=100
```

## Публикация UI

Базовая проверка выполняется через port-forward. Если нужно открыть UI
в локальной сети станции через Traefik, включи ingress в отдельном
site-specific values-файле, например `infra/k8s/values/station.yaml`.

```yaml
rescue-ai-api:
  ingress:
    enabled: true
    className: traefik
    hosts:
      - host: rescue-ai.station.local
        paths:
          - path: /
            pathType: Prefix
```

Применение:

```bash
helm upgrade rescue-ai infra/k8s/charts/rescue-ai \
    -n rescue-ai \
    -f infra/k8s/values/offline.yaml \
    -f infra/k8s/values/station.yaml
```

## Ротация секретов

Ротация делается в Vault, затем перезапускаются затронутые workloads.
Например для MinIO:

```bash
kubectl -n vault port-forward svc/rescue-ai-vault 8200:8200 &
export VAULT_ADDR=http://127.0.0.1:8200
export VAULT_TOKEN='<operator-token>'

vault kv put secret/rescue-ai/minio \
    MINIO_ROOT_USER='rescueadmin' \
    MINIO_ROOT_PASSWORD='<new-minio-root-password>'

vault kv patch secret/rescue-ai/api \
    ARTIFACTS_S3_ACCESS_KEY_ID='rescueadmin' \
    ARTIFACTS_S3_SECRET_ACCESS_KEY='<new-minio-root-password>'

kubectl -n rescue-ai rollout restart statefulset rescue-ai-minio
kubectl -n rescue-ai rollout restart deployment rescue-ai-rescue-ai-api
```

Для Postgres пароль надо менять и в самой БД (`ALTER USER`), и в Vault
`secret/rescue-ai/postgresql` / `secret/rescue-ai/api` /
`secret/rescue-ai/sync-worker`, затем делать rollout.

## Обновление образов

```bash
for svc in api detection nav-engine sync-worker; do
    sudo k3s ctr images import "rescue-ai-$svc-<tag>.tar"
done

helm upgrade rescue-ai infra/k8s/charts/rescue-ai \
    -n rescue-ai \
    -f infra/k8s/values/offline.yaml \
    --set rescue-ai-api.image.tag=<tag> \
    --set rescue-ai-detection.image.tag=<tag> \
    --set rescue-ai-nav-engine.image.tag=<tag> \
    --set rescue-ai-sync-worker.image.tag=<tag>
```

## Vault после перезапуска станции

После reboot или пересоздания `rescue-ai-vault-0` Vault снова будет
`Sealed: true`. Выполни unseal тремя ключами. Пока Vault sealed,
workloads с Vault Agent не смогут стартовать или обновить секреты.

## Откат

```bash
helm history rescue-ai -n rescue-ai
helm rollback rescue-ai <REVISION> -n rescue-ai
```

Если откат возвращает старый тег образа, этот образ должен уже быть в
containerd k3s.

## Полная очистка

```bash
helm uninstall rescue-ai -n rescue-ai
helm uninstall rescue-ai-vault -n vault
kubectl delete namespace rescue-ai
kubectl delete namespace vault
sudo /usr/local/bin/k3s-uninstall.sh
```

Удаление namespaces удалит PVC локальных Postgres / MinIO (`rescue-ai`)
и Vault (`vault`). Перед этим сделай бэкап, если данные нужны.

## Чек-лист

- [ ] `kubectl get nodes` показывает одну ноду `Ready`.
- [ ] Vault `Initialized: true`, `Sealed: false`.
- [ ] `kubectl -n rescue-ai get secret` не показывает credential
      Secrets Rescue-AI.
- [ ] `kubectl -n rescue-ai get pods` показывает все workloads
      `Running`/`Completed`.
- [ ] `curl http://127.0.0.1:8000/health` возвращает 200.
- [ ] API, Postgres и MinIO имеют файлы в `/vault/secrets/*`.
- [ ] `SELECT count(*) FROM app.replication_outbox` выполняется.
- [ ] Тестовая миссия через UI создаёт точки в
      `app.auto_trajectory_points`.
- [ ] Для hybrid: sync-worker не показывает постоянных ошибок
      подключения к remote-контуру.

## Связанные документы

- [ADR-0007 — Offline / Hybrid профили](../adr/ADR-0007-autonomous-deployment-and-offline-sync.md)
- [ADR-0008 — Kubernetes и Vault](../adr/ADR-0008-kubernetes-and-secrets.md)
- [Vault setup](vault_setup.md)
- [Настройка mTLS на Raspberry Pi](rpi_mtls_setup.md)
