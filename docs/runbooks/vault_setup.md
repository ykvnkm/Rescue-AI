# Vault setup — пошагово для обоих прод-режимов

Этот runbook — пошаговая инструкция «как развернуть HashiCorp Vault и
подключить к нему все сервисы Rescue-AI», написанная для тех, кто
Vault никогда не трогал. Идём от пустого кластера до момента, когда
локальные Postgres/MinIO, `rescue-ai-api`, `rescue-ai-sync-worker`,
а в cloud ещё `rescue-ai-batch-exporter` и Airflow читают свои секреты
прямо из Vault без единого DSN или storage credential в
`kubectl get secret`. Batch exporter в offline/k3s не запускается.

## Зачем нам Vault, в одной фразе

Прикладные секреты (DSN базы, ключи S3, connection-объекты Airflow)
не должны лежать ни в репозитории, ни в `kubectl get secret` в открытом
виде, ни в переменных окружения CI. Vault даёт три вещи:

1. **Централизованное хранение KV-секретов** с ACL по ServiceAccount.
2. **Vault Agent Injector** — мутирующий webhook, который вшивает
   sidecar-контейнер в каждый под и рендерит секреты в файлы
   `/vault/secrets/*` прямо в памяти пода. Приложение читает env
   из этого файла.
3. **Vault Secrets Backend для Airflow** — connection-объекты живут в
   KV, не в `airflow connections add`.

Что **остаётся** в GitHub Secrets (это правильно, по учебнику):
`GHCR_USERNAME`, `GHCR_TOKEN`, `SERVER_HOST`, `SERVER_USER`,
`SERVER_SSH_KEY`. Это **bootstrap-секреты** — без них мы не доберёмся
до сервера, где живёт Vault. Курица-и-яйцо.

## Что мы развернём

| Профиль  | Где живёт Vault                            | Mode               | Storage             | HA   |
|----------|--------------------------------------------|--------------------|---------------------|------|
| offline  | отдельный Helm-релиз `rescue-ai-vault` в namespace `vault` | standalone, file   | PVC 1Gi (local-path)| no   |
| cloud    | отдельный Helm-релиз `rescue-ai-vault` в namespace `vault` | standalone, file   | PVC 10Gi (default SC) | no |

Архитектурно **одинаково в обоих профилях**: Vault — отдельный helm
release в отдельном namespace `vault`. Это соответствует best practice
(HashiCorp, CIS K8s Benchmark §5.7.1): security-инфраструктура
изолирована от прикладного контура по RBAC и blast radius, независимо
от количества узлов кластера.

HA на 3+ узлов Raft — следующий шаг роста, документирован в самом
конце («Дальнейший рост»). Для дипломной защиты одноузлового Vault
достаточно: данные на PVC, snapshot бэкапится в S3.

---

## Часть 1 — Offline (k3s на станции)

### 1.1. Предусловия

- k3s уже установлен и запущен (см.
  [`k3s_field_deploy.md`](k3s_field_deploy.md)).
- Namespaces `vault` и `rescue-ai` создаются перед install (см. ниже);
  umbrella-чарт приложения ставится **после** того, как Vault поднят,
  распечатан и инициализирован.
- Helm CLI установлен на ноутбуке оператора, `KUBECONFIG` указывает
  на k3s.
- `vault` CLI установлен локально (`brew install vault` или скачать
  бинарь с releases.hashicorp.com).
- Дефолтный `StorageClass` (local-path) активен:
  ```bash
  kubectl get storageclass
  # должен быть один помечен (default)
  ```

### 1.2. Установка Vault отдельным релизом

```bash
kubectl create namespace vault --dry-run=client -o yaml \
    | kubectl apply -f -

helm repo add hashicorp https://helm.releases.hashicorp.com
helm repo update

helm upgrade --install rescue-ai-vault hashicorp/vault \
    -n vault \
    --version 0.30.0 \
    -f infra/k8s/vault/values-offline.yaml
```

После этого выполняются init/unseal и bootstrap. Только потом
создаётся namespace `rescue-ai` и ставится umbrella-чарт приложения.

### 1.3. Init + unseal

Vault при первом старте находится в состоянии **sealed** — данные
зашифрованы, ключи дешифрования ещё не созданы. `init` создаёт
master-ключ Шамира (5 фрагментов, для распечатки нужно 3), `unseal`
вводит эти фрагменты.

**Init (выполняется ОДИН РАЗ за всю жизнь инсталляции):**

```bash
kubectl -n vault exec -it rescue-ai-vault-0 -- \
    vault operator init -key-shares=5 -key-threshold=3
```

Вывод выглядит так:
```
Unseal Key 1: 8w...
Unseal Key 2: kT...
Unseal Key 3: mP...
Unseal Key 4: bC...
Unseal Key 5: zR...

Initial Root Token: hvs.XYZ...
```

> **КРИТИЧНО:** Запиши ВСЕ 5 unseal keys и root token в надёжное
> место (1Password, hardware token, бумажный конверт в сейфе).
> Никогда не коммить в git. Без 3 из 5 unseal keys ты потеряешь
> доступ ко ВСЕМ секретам навсегда — это by design.

**Unseal (выполняется при каждом перезапуске пода Vault):**

```bash
kubectl -n vault exec -it rescue-ai-vault-0 -- vault operator unseal <key-1>
kubectl -n vault exec -it rescue-ai-vault-0 -- vault operator unseal <key-2>
kubectl -n vault exec -it rescue-ai-vault-0 -- vault operator unseal <key-3>
```

После третьего `unseal` Vault переходит в `Sealed: false`. Проверь:
```bash
kubectl -n vault exec -it rescue-ai-vault-0 -- vault status
# Sealed: false
# Initialized: true
```

### 1.4. Bootstrap (политики, роли, секреты)

Скрипт `scripts/security/vault_bootstrap.sh` за один проход:
- включает KV v2 на `secret/`,
- включает Kubernetes auth method,
- загружает политики для local infra и приложений:
  `rescue-ai-postgresql`, `rescue-ai-minio`, `rescue-ai-api`,
  `rescue-ai-sync-worker`; в cloud дополнительно
  `rescue-ai-batch-exporter`,
- создаёт role'и 1:1 с ServiceAccount-ами подов,
- пишет реальные значения infra/app/mTLS секретов в KV.

Запуск:

```bash
# Перед bootstrap-ом создаём namespace для приложения (но без
# helm install пока — bootstrap должен пройти до старта подов).
kubectl create namespace rescue-ai --dry-run=client -o yaml \
    | kubectl apply -f -

kubectl -n vault port-forward svc/rescue-ai-vault 8200:8200 &

# Подставь реальные значения (из 1Password или хранилища паролей).
VAULT_ADDR=http://127.0.0.1:8200 \
VAULT_TOKEN=hvs.XYZ... \
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
DB_DSN='postgresql://rescue:<password>@rescue-ai-postgresql.rescue-ai.svc.cluster.local:5432/rescue_ai' \
ENABLE_RPI_MTLS=true \
MTLS_CA_CERT_FILE=scripts/security/out/station-root-ca.crt \
MTLS_CLIENT_CERT_FILE=scripts/security/out/gcs-client.crt \
MTLS_CLIENT_KEY_FILE=scripts/security/out/gcs-client.key \
DEPLOYMENT_REMOTE_DB_DSN='postgresql://rescue@<cloud-host>:5432/rescue_ai' \
DEPLOYMENT_REMOTE_S3_ACCESS_KEY_ID='<yandex-key-id>' \
DEPLOYMENT_REMOTE_S3_SECRET_ACCESS_KEY='<yandex-secret>' \
    ./scripts/security/vault_bootstrap.sh

# Остановить port-forward
kill %1
```

После успешного запуска поды rescue-ai-* (после `helm install
rescue-ai`) перейдут в `Running` — init-контейнеры `vault-agent-init`
отрисуют `/vault/secrets/app.env` и приложения стартуют. Локальные
Postgres/MinIO также стартуют через Vault Agent, который отрисует
`/vault/secrets/postgres-password` и `/vault/secrets/minio-root-password`.

Проверь:
```bash
kubectl -n rescue-ai get pods
# Все pods в Running 2/2 или 3/3 (sidecar vault-agent)
kubectl -n rescue-ai exec deploy/rescue-ai-rescue-ai-api -- \
    cat /vault/secrets/app.env
# DB_DSN=postgresql://... ARTIFACTS_S3_*=...

kubectl -n rescue-ai get secret
# Не должно быть rescue-ai-postgresql-auth, rescue-ai-minio-auth,
# rpi-mtls, rescue-ai-api-secret, rescue-ai-sync-worker-secret.
```

### 1.5. Что делать при рестарте Vault-пода

k3s может перезапустить Vault-под (kernel upgrade, node reboot,
ручной `kubectl delete pod`). После рестарта Vault снова `Sealed`.
Это не катастрофа — но прикладные поды не смогут получить секреты,
пока ты не выполнишь unseal заново.

**Признаки sealed Vault:**
- `vault status` → `Sealed: true`
- Прикладные поды в `Init:0/2` или `CrashLoopBackOff`
- В логах api: `vault-agent-init: error: Vault is sealed`

**Что делать:**
```bash
for key in <key-1> <key-2> <key-3>; do
    kubectl -n vault exec rescue-ai-vault-0 -- vault operator unseal $key
done
```

Поды rescue-ai-* поднимутся сами в течение минуты.

> **Автоматический unseal** в проде делается через KMS (AWS KMS, GCP
> Cloud KMS, HashiCorp Cloud Platform). В оффлайн-станции без
> интернета KMS недоступен → unseal остаётся ручным. Это **не баг**,
> это обязательная безопасность: оператор станции должен иметь
> физический контроль над unseal-ключами.

### 1.6. Бэкап

Снимок Vault: каталог `/vault/data` внутри пода = это весь стейт.
Бэкап один раз в день при наличии связи (вручную):

```bash
kubectl -n vault exec rescue-ai-vault-0 -- \
    tar czf - /vault/data > vault-backup-$(date +%F).tar.gz
# Положить в S3 (offline → remote через sync-worker уже умеет; либо
# scp вручную при смене смены).
```

Восстановление: остановить Vault, развернуть tar обратно в PVC, поднять
Vault, выполнить unseal. Документировано в hashicorp/vault-helm README,
секция «Backup and restore».

---

## Часть 2 — Cloud (managed K8s)

### 2.1. Отличия от offline

Архитектура Vault в cloud **идентична** offline: тот же helm-release,
тот же namespace, тот же values-набор по структуре. Отличаются только:

| Что                    | offline                          | cloud                                  |
|------------------------|----------------------------------|----------------------------------------|
| values-файл            | `values-offline.yaml`            | `values-cloud.yaml` (больше PVC, Ingress) |
| Доступ к UI            | port-forward                     | Ingress + cert-manager TLS             |
| Auto-unseal            | ручной (KMS недоступен)          | опционально через Yandex/AWS KMS       |
| Airflow Secrets Backend | не используется                 | используется (Airflow живёт только в cloud) |

### 2.2. Установка Vault

В cloud Vault ставится с другими values
(`infra/k8s/vault/values-cloud.yaml`), но в тот же `vault` namespace
тем же release-ом `rescue-ai-vault`. Это правильный паттерн:
security-инфраструктура одинаково изолирована в обоих профилях.

```bash
kubectl create namespace vault
helm repo add hashicorp https://helm.releases.hashicorp.com
helm repo update

helm install rescue-ai-vault hashicorp/vault \
    -n vault \
    -f infra/k8s/vault/values-cloud.yaml
```

После этого umbrella-чарт `rescue-ai` ставится с `vault.enabled: false`
(дефолт cloud.yaml) и не пытается развернуть свой Vault.

### 2.3. Init + unseal — то же, что в offline

```bash
kubectl -n vault exec -it rescue-ai-vault-0 -- \
    vault operator init -key-shares=5 -key-threshold=3
# Запиши 5 unseal keys и root token (1Password)

kubectl -n vault exec -it rescue-ai-vault-0 -- vault operator unseal <key-1>
kubectl -n vault exec -it rescue-ai-vault-0 -- vault operator unseal <key-2>
kubectl -n vault exec -it rescue-ai-vault-0 -- vault operator unseal <key-3>
```

> **Auto-unseal на managed K8s.** Если хочется автоматический unseal
> через managed KMS (Yandex Cloud KMS / AWS KMS), в `values-cloud.yaml`
> в `server.standalone.config` добавь секцию `seal "awskms" {...}`
> и пересоздай Vault. Это превратит ручной unseal в автоматический
> при рестарте пода. Не обязательно для дипломки.

### 2.4. Bootstrap с Airflow

В cloud bootstrap-скрипт дополнительно настраивает Airflow Secrets
Backend (`ENABLE_AIRFLOW=true`):

```bash
kubectl -n vault port-forward svc/rescue-ai-vault 8200:8200 &

VAULT_ADDR=http://127.0.0.1:8200 \
VAULT_TOKEN=hvs.XYZ... \
NAMESPACE=rescue-ai \
VAULT_NAMESPACE=vault \
VAULT_SERVICE_ACCOUNT=rescue-ai-vault \
ENABLE_LOCAL_INFRA=false \
ENABLE_BATCH_EXPORTER=true \
DB_DSN='postgresql://rescue@rescue-app-pgsql.mdb.yandexcloud.net:6432/rescue_ai' \
ARTIFACTS_S3_ACCESS_KEY_ID='<yc-static-key-id>' \
ARTIFACTS_S3_SECRET_ACCESS_KEY='<yc-static-secret>' \
ENABLE_AIRFLOW=true \
AIRFLOW_DB_CONN='postgresql://rescue@rescue-app-pgsql.mdb.yandexcloud.net:6432/rescue_ai' \
AIRFLOW_S3_CONN='{"conn_type":"aws","login":"<yc-static-key-id>","password":"<yc-static-secret>","extra":{"endpoint_url":"https://storage.yandexcloud.net","region_name":"ru-central1","bucket":"rescue-ai-mission-artifacts","prefix":"missions"}}' \
    ./scripts/security/vault_bootstrap.sh

kill %1
```

> **Почему две переменных `NAMESPACE` и `VAULT_NAMESPACE`?**
> - `NAMESPACE=rescue-ai` — где живут поды приложения. К SA из этого
>   namespace будут привязываться role'и (`bound_service_account_namespaces`).
> - `VAULT_NAMESPACE=vault` — где живёт сам Vault. Скрипт идёт сюда за
>   `token_reviewer_jwt` — токеном, под которым Vault опрашивает
>   kube-apiserver.
> В обоих профилях (offline и cloud) они разные — `rescue-ai` и
> `vault` соответственно.

### 2.5. Airflow и Vault: как это работает изнутри

1. DAG-таска зовёт `BaseHook.get_connection("rescue_app_db")`.
2. Airflow смотрит на `AIRFLOW__SECRETS__BACKEND` → видит
   `VaultBackend`.
3. VaultBackend поднимается под ServiceAccount-ом пода Airflow
   (`rescue-ai-airflow-scheduler` или `…-worker`), берёт JWT-токен.
4. Идёт в `http://rescue-ai-vault.vault.svc.cluster.local:8200`,
   проходит Kubernetes auth с этим JWT.
5. Vault через TokenReviewer API проверяет JWT в k8s API, видит,
   что под живёт под SA `rescue-ai-airflow-*`, и выдаёт токен с
   политикой `rescue-ai-airflow`.
6. VaultBackend читает `secret/data/airflow/connections/rescue_app_db`,
   получает поле `conn_uri` и возвращает `Connection`-объект.

Никаких `secretKeyRef` в Airflow Helm values больше нет.

### 2.6. Что остаётся в k8s Secret в cloud

| Секрет                                    | Где живёт         | Почему не в Vault                          |
|-------------------------------------------|-------------------|--------------------------------------------|
| GHCR pull credentials (`ghcr-pull`)       | k8s Secret (создаёт CI) | k8s сам подтягивает images, до Vault не доберётся |
| Airflow metadata DB URI (`rescue-airflow-meta-db`) | k8s Secret (создаёт CI из GH Secret `AIRFLOW_DB_URI`) | Airflow стартует ДО Vault — chicken-and-egg. Подключается через `data.metadataSecretName` в cloud.yaml |
| Vault unseal keys, root token             | вне кластера (1Password) | Никогда не лежат в k8s             |
| Wildcard TLS-сертификат для Ingress       | cert-manager → k8s Secret | k8s ingress-controller читает Secret напрямую |

Все остальные секреты (DB DSN приложения, S3-ключи, Airflow
connections) — в Vault.

В offline/k3s локальные Postgres, MinIO и mTLS material тоже не
используют Kubernetes Secret. Их значения живут в Vault KV и
рендерятся Vault Agent'ом в файлы внутри pod'ов.

---

## Часть 3 — Что увидит экзаменатор

Когда защитник скажет «секреты в Vault, не в Secrets» — это можно
доказать тремя командами:

```bash
# 1. В кластере НЕТ credential Secret'ов Rescue-AI
kubectl -n rescue-ai get secret
# Offline: не должно быть rescue-ai-postgresql-auth,
# rescue-ai-minio-auth, rpi-mtls, rescue-ai-api-secret,
# rescue-ai-sync-worker-secret.
# Cloud: допустимы ghcr-pull, TLS/cert-manager, Airflow metadata DB.

# 2. В подах sidecar vault-agent рендерит секреты
kubectl -n rescue-ai exec deploy/rescue-ai-rescue-ai-api -c vault-agent -- \
    ls /vault/secrets/
# app.env

# 3. Конфиг приложения берёт env-переменные из файла перед exec python
kubectl -n rescue-ai exec deploy/rescue-ai-rescue-ai-api -c api -- \
    sh -c 'grep DB_DSN /vault/secrets/app.env'
# DB_DSN=postgresql://... — но в манифесте deployment.yaml НЕТ env с этим.
```

Граф потока (упростим, для слайда):

```
┌─────────────┐  helm install   ┌──────────────┐
│ Operator    │ ───────────────►│ Vault (init  │  unseal keys + root → 1Password
│ (человек)   │   vault unseal  │   sealed)    │
└─────────────┘                 └──────────────┘
       │
       │ vault_bootstrap.sh
       ▼
┌──────────────────────────────────────────┐
│ Vault                                    │
│ ├ KV v2: infra + app + mtls secrets      │
│ ├ k8s auth: roles ↔ ServiceAccounts      │
│ └ policies: pg / minio / api / worker    │
└──────────────────────────────────────────┘
       ▲
       │ kubernetes auth (JWT)
       │
┌──────────────────┐
│ Pod              │ ◄── injected sidecar ──┐
│ ├ rescue-ai-api  │                        │
│ ├ ServiceAccount │ /vault/secrets/app.env │
│ └ container      │                        │
└──────────────────┘                        │
       │                                    │
       └── set -a && . /vault/secrets/app.env
           exec python -m rescue_ai…
```

---

## Часть 4 — Дальнейший рост

Что сделать, когда дипломка позади и Vault уйдёт в реальную
эксплуатацию:

1. **Raft HA на 3+ узлов** — Vault Helm chart с `server.ha.enabled: true`
   и `server.ha.raft.enabled: true`. Каждый узел держит копию данных,
   `vault operator raft join` подключает новых.
2. **Auto-unseal через KMS** — `seal "awskms"` или `seal "gcpckms"`
   секция в server config. Vault сам распечатывается при старте.
3. **Periodic snapshots** в S3** — `vault operator raft snapshot save`
   из cron-таски, шифровать и заливать в bucket.
4. **Audit log в файл** — `vault audit enable file file_path=/vault/audit/log`.
   У нас уже есть `auditStorage: enabled: true` (5Gi PVC).
5. **Rotation root token** — после первоначальной настройки сгенерировать
   через `vault operator generate-root` свежий root, старый отозвать.
6. **PKI engine** — Vault как CA для mTLS внутри кластера, заменит
   ручной `scripts/security/gen_*.sh`.

Это всё **за рамками дипломки**, но если экзаменатор спросит
«а что дальше» — ответ выше.

---

## Связанные документы

- [ADR-0008 — Kubernetes и Vault](../adr/ADR-0008-kubernetes-and-secrets.md)
- [k3s field deploy](k3s_field_deploy.md) — что делать на станции до Vault
- [vault_bootstrap.sh](../../scripts/security/vault_bootstrap.sh) — единая точка bootstrap
- [policies/](../../infra/k8s/vault/policies/) — per-role HCL-политики
- [values-cloud.yaml](../../infra/k8s/vault/values-cloud.yaml) — Helm values для отдельного Vault в cloud
