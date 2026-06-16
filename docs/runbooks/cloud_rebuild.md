# Cloud rebuild — пересоздание удалённого контура с нуля на новых серверах

Назначение: если VPS на reg.ru удалят / пересоздадут (а у нас стоп/старт
ради экономии), этот документ позволяет **развернуть весь cloud-контур
заново на других серверах**, в том числе с новыми IP и новой managed-БД,
и довести до зелёного `https://api.rescue-ai.ru/health`.

> Перед защитой: пройти этот runbook ОДИН раз заранее на свежих серверах,
> а не в день Х. На полный прогон закладывай ~1.5–2 часа.

Связанные документы (не дублируем — ссылаемся):
- Vault детально — [`vault_setup.md`](vault_setup.md)
- Правка секретов Vault — [`VAULT_SECRETS_EDIT.md`](VAULT_SECRETS_EDIT.md)
- Мониторинг — [`observability_setup.md`](observability_setup.md)
- Batch / Airflow — [`batch_namespace.md`](batch_namespace.md), [`batch_operations.md`](batch_operations.md)
- Подъём кластера после рестарта — [`../../scripts/ops/cloud_up.sh`](../../scripts/ops/cloud_up.sh)

---

## 0. Модель: что переносится, а что создаётся заново

### 0.1. ПЕРЕНОСИМ как есть (reusable — НЕ меняется при смене серверов)

Это «активы», которые надо сохранить и иметь под рукой. Если серверы
сотрут — эти вещи остаются валидными.

| Актив | Где лежит | Зачем |
|---|---|---|
| Репозиторий (весь код, чарты, values, workflow) | GitHub `ykvnkm/rescue-ai` | источник истины деплоя |
| **Yandex KMS-ключ** `kms_key_id = <YOUR_KMS_KEY_ID>` | Yandex Cloud (KMS) | auto-unseal Vault — переживает смерть серверов |
| **`authorized_key.json`** (SA `ajebm4q81ar3r8iku57o`, роль `kms.keys.encrypterDecrypter`) | `scripts/security/out/authorized_key.json` (gitignored) + бэкап в 1Password | Vault логинится в Yandex KMS |
| Домен `rescue-ai.ru` | регистратор домена | ingress + TLS |
| GHCR pull-token | GitHub Secret `GHCR_TOKEN` + 1Password | тянуть образы из ghcr.io |
| Yandex S3 ключи (artifacts) | Vault / 1Password | хранилище артефактов миссий |
| SMTP app-password (`<your-email@example.com>`) | Vault / 1Password | алерты Alertmanager |
| Grafana admin-пароль | Vault / 1Password | доступ к дашбордам |

> **Ключевой выигрыш auto-unseal через Yandex KMS:** механизм распечатки
> не привязан к серверам. На новом кластере новый Vault, указывающий на
> ТОТ ЖЕ `kms_key_id` + `authorized_key.json`, распечатывается сам. Но
> *данные* Vault (секреты) — новые, их надо забутстрапить заново (шаг 5).

### 0.2. СОЗДАЁМ заново / ОБНОВЛЯЕМ (меняется при смене серверов/БД)

| Что | Откуда берётся | Куда вписать |
|---|---|---|
| 3× публичный/приватный IP новых VPS | reg.ru панель | DNS, `cloud_up.sh`, GitHub Secret `SERVER_HOST` |
| Non-root deploy-пользователь + его SSH-ключ | создаёшь на новых VPS | GitHub Secret `SERVER_SSH_KEY`, `SERVER_USER` |
| Реквизиты managed-БД (host/port/user/pass) — **если БД новая** | reg.ru managed Postgres | Vault `secret/rescue-ai/api` (`DB_DSN`), GitHub Secret `AIRFLOW_DB_URI`, при смене порта — `extraEgressPorts` в `cloud.yaml` |
| Vault **recovery-ключи** + root-токен | `vault operator init` на новом Vault | `scripts/security/out/vault-init-cloud.json` + 1Password |
| Let's Encrypt сертификаты | cert-manager выпишет сам после DNS | — (автоматически) |
| DNS A-записи `api.` / `vault.` / `grafana.` rescue-ai.ru | ты в панели домена | → публичный IP нового control-plane |

---

## 1. Серверы (reg.ru): 3 VPS

1. Создать **3 виртуальных сервера** (не managed k8s!): 1 control-plane +
   2 agent. CP — не меньше 4 GB RAM (на нём Vault + ingress + cert-manager),
   agents — по нагрузке. Все в **одной приватной сети** reg.ru (общий
   сегмент `192.168.0.0/24` или аналог).
2. На КАЖДОМ сервере:
   ```bash
   # под root, разово:
   adduser rescue && usermod -aG sudo rescue
   mkdir -p /home/rescue/.ssh && chmod 700 /home/rescue/.ssh
   # вставить публичный SSH-ключ deploy-пользователя:
   #   echo 'ssh-ed25519 AAAA... deploy' > /home/rescue/.ssh/authorized_keys
   chmod 600 /home/rescue/.ssh/authorized_keys
   chown -R rescue:rescue /home/rescue/.ssh
   ```
   Root-вход НЕ отключаем, но работаем под `rescue`.
3. **Выключить IPv6** (на reg.ru он сломан и ломает helm/git к внешним
   CDN). На каждом сервере:
   ```bash
   sudo sysctl -w net.ipv6.conf.all.disable_ipv6=1
   sudo sysctl -w net.ipv6.conf.default.disable_ipv6=1
   # закрепить в /etc/sysctl.d/99-disable-ipv6.conf
   ```

---

## 2. k3s: control-plane + 2 agent

На **control-plane**:
```bash
curl -sfL https://get.k3s.io | sh -
sudo cat /var/lib/rancher/k3s/server/node-token   # → TOKEN
# публичный/приватный IP CP → CP_IP
```
На каждом **agent**:
```bash
curl -sfL https://get.k3s.io | K3S_URL=https://<CP_IP>:6443 K3S_TOKEN=<TOKEN> sh -
```
Проверка на CP:
```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl get nodes        # 3 ноды Ready
```
> k3s включает flannel + kube-router, который **энфорсит NetworkPolicy**
> (в отличие от ванильного flannel) — это важно, наши чарты на это
> рассчитывают. Подробнее — [`k3s_field_deploy.md`](k3s_field_deploy.md).

---

## 3. ingress-nginx + cert-manager + ClusterIssuer

> ⚠️ **Geo-block reg.ru:** `helm repo add hashicorp/jetstack` и hashicorp
> CDN отдают **403** на российский IP. Поэтому ставим из **github static
> manifests / clone**, НЕ через helm-репозитории hashicorp/jetstack.
> ghcr.io / docker.io / quay.io / github — доступны.

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# ingress-nginx (static manifest с github)
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.11.3/deploy/static/provider/cloud/deploy.yaml

# cert-manager v1.16.3 (static manifest с github)
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.16.3/cert-manager.yaml
kubectl -n cert-manager rollout status deploy/cert-manager-webhook --timeout=180s
```
ClusterIssuer Let's Encrypt (HTTP-01 через ingress-nginx):
```bash
kubectl apply -f - <<'EOF'
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: <your-email@example.com>
    privateKeySecretRef:
      name: letsencrypt-prod
    solvers:
      - http01:
          ingress:
            class: nginx
EOF
```

---

## 4. DNS

В панели домена `rescue-ai.ru` создать/обновить **A-записи** на
**публичный IP нового control-plane**:
```
api.rescue-ai.ru      A   <public IP control-plane>
vault.rescue-ai.ru    A   <public IP control-plane>
grafana.rescue-ai.ru  A   <public IP control-plane>   # если включаем Grafana ingress
```
Дождаться распространения (`dig api.rescue-ai.ru +short`). Без корректного
DNS cert-manager не выпишет сертификаты (HTTP-01 не пройдёт).

---

## 5. Vault + auto-unseal через Yandex KMS

Подробности и обоснование — [`vault_setup.md`](vault_setup.md), здесь —
последовательность для rebuild.

> ⚠️ Vault с поддержкой `seal "yandexcloudkms"` есть только в **форке
> Yandex** (upstream hashicorp/vault такого seal не имеет). Ставим из
> **Yandex OCI-чарта** (та же база vault-helm 0.30.0). Чарт и образ
> публичные, пуллятся с reg.ru напрямую — **проверено** (вариант A).
> SA-ключ отдаём чарту через `--set-file yandexKmsAuthJson=...`; чарт сам
> кладёт его в Secret `kms-creds`. **Секрет руками не создавай** — helm
> перетрёт его пустым при upgrade. Все KMS-параметры —
> в [`infra/k8s/vault/values-cloud-kms.yaml`](../../infra/k8s/vault/values-cloud-kms.yaml).

**Вся настройка Vault — ОДНОЙ командой** через
[`scripts/ops/cloud_bootstrap.sh`](../../scripts/ops/cloud_bootstrap.sh)
(helm install + init + auto-unseal + политики/роли/секреты). Запускать на
control-plane из корня репо:

```bash
# 1) положи рядом (оба gitignored, в scripts/security/out/):
#      authorized_key.json          (SA-ключ Yandex для KMS)
#      cloud.env                    (cp scripts/security/cloud.env.example → заполни)
# 2) одна команда:
./scripts/ops/cloud_bootstrap.sh
```
Скрипт идемпотентен: повторный запуск не переинициализирует Vault.
`YC_KMS_KEY_ID` и все секреты берутся из `cloud.env` (вне git), ID ключа
в репозитории НЕ хранится.

<details><summary>Что делает по шагам (если нужно вручную)</summary>

1. `helm upgrade --install` Yandex yckms-чарта (+ `--set YANDEXCLOUD_KMS_KEY_ID`
   и `--set-file yandexKmsAuthJson`);
2. `vault operator init -recovery-shares=5 -recovery-threshold=3` (на чистом
   кластере — сразу с KMS-seal, миграция НЕ нужна) → recovery-ключи + root-токен
   в `scripts/security/out/vault-init-cloud.json` (положи в 1Password!);
3. ждёт auto-unseal через KMS (человек не нужен);
4. `vault_bootstrap.sh` — KV v2, k8s-auth, политики, роли, секреты.

> Перенос СУЩЕСТВУЮЩЕГО shamir-Vault на KMS (как на исходном кластере) —
> это `vault operator unseal -migrate <shamir-key>` × threshold после
> установки yckms-образа. На чистом rebuild это не нужно.
> Recovery-ключи нужны лишь для редких операций (rekey, generate-root); на рестарте человек не
> требуется.
</details>

**5.3. (ручной аналог bootstrap)** — то, что делает `cloud_bootstrap.sh`
шаг 4, если запускать руками: `vault_bootstrap.sh` с актуальными
значениями **новой БД** и переносимыми S3/SMTP/Grafana:
```bash
# туннель к Vault
kubectl -n vault port-forward svc/rescue-ai-vault 8200:8200 &
export VAULT_ADDR=http://127.0.0.1:8200
export VAULT_TOKEN=<root-token из 5.2>

ENABLE_LOCAL_INFRA=false \
ENABLE_AIRFLOW=true \
ENABLE_ALERTMANAGER=true \
ENABLE_GRAFANA=true \
DB_DSN='postgresql://<user>:<pass>@<НОВЫЙ host>:<port>/<db>' \
ARTIFACTS_S3_ENDPOINT='https://storage.yandexcloud.net' \
ARTIFACTS_S3_ACCESS_KEY_ID='<yandex S3 key>' \
ARTIFACTS_S3_SECRET_ACCESS_KEY='<yandex S3 secret>' \
ALERTMANAGER_SMTP_PASSWORD='<gmail app password>' \
GRAFANA_ADMIN_PASSWORD='<grafana pass>' \
    bash scripts/security/vault_bootstrap.sh
```
> Cloud-демо открытое: `API_AUTH_TOKEN` оставить пустым
> (`vault kv patch secret/rescue-ai/api API_AUTH_TOKEN=''`), иначе закроется
> публичный доступ к API. См. `cloud.yaml` (rateLimitPerMin вместо токена).

**5.4. Включить Vault UI ingress** — DNS из шага 4 должен резолвиться;
cert-manager выпишет `vault-tls` автоматически. Консоль:
`https://vault.rescue-ai.ru`.

---

## 6. GitHub Secrets (обновить под новые серверы/БД)

Settings → Secrets and variables → Actions. Обновить/проверить:

| Secret | Значение при rebuild |
|---|---|
| `SERVER_HOST` | **новый** публичный IP control-plane |
| `SERVER_USER` | `rescue` |
| `SERVER_SSH_KEY` | приватный SSH-ключ deploy-пользователя |
| `GHCR_USERNAME` | `ykvnkm` (не меняется) |
| `GHCR_TOKEN` | PAT с `read:packages` (переносимый) |
| `AIRFLOW_DB_URI` | `postgresql://...@<НОВЫЙ host>:<port>/airflow` |

На control-plane разово клонировать репо в `~/rescue-ai` под `rescue`
(deploy.yml делает `git fetch` в этот каталог):
```bash
git clone https://github.com/ykvnkm/rescue-ai.git ~/rescue-ai
```

---

## 7. Деплой приложения + мониторинга через CI/CD

Слить в `main` (или запустить вручную **Actions → deploy →
workflow_dispatch**). `deploy.yml`:
1. собирает 5 образов (api / detection / nav-engine / batch-worker /
   batch-exporter) в GHCR;
2. по SSH на CP: `helm upgrade --install rescue-ai` с `cloud.yaml`;
3. ставит контур мониторинга (`rescue-ai-observability` в namespace
   `monitoring`) тем же пайплайном;
4. проверяет, что Vault Agent отрендерил секреты, и health api.

> Vault **намеренно не деплоится** этим пайплайном (security-инфра, свой
> lifecycle — шаги 5.x выше). Это правильный паттерн, не пропуск.

---

## 8. Batch (Airflow) — отдельный release

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl create namespace rescue-batch --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install rescue-batch infra/k8s/charts/rescue-batch \
    -n rescue-batch -f infra/k8s/values/rescue-batch-cloud.yaml \
    --set rescue-ai-batch-exporter.image.repository="ghcr.io/ykvnkm/rescue-ai-batch-exporter" \
    --set rescue-ai-batch-exporter.image.tag="<GIT_SHA>" \
    --set "airflow.env[0].value=ghcr.io/ykvnkm/rescue-ai-batch-worker:<GIT_SHA>"
```
Детали — [`batch_namespace.md`](batch_namespace.md).

---

## 9. Проверка (приёмка rebuild)

```bash
curl -fsS https://api.rescue-ai.ru/health     # {"status":"ok"}
curl -fsS https://api.rescue-ai.ru/ready      # database:true, storage:true
# Vault auto-unseal: после рестарта пода Vault поднимается Sealed:false без рук
kubectl -n vault exec rescue-ai-vault-0 -- vault status | grep Sealed   # false
# Поды разнесены по нодам (FT):
kubectl -n rescue-ai get pods -o wide
# Мониторинг:
kubectl -n monitoring get pods
```
После rebuild **не нужно** запускать ручную распечатку — auto-unseal.
`cloud_up.sh` теперь только ждёт k3s и пинает не-Ready поды (распечатка
делается KMS автоматически).

---

## 10. Что передать (handover checklist)

Если пересобирать будет другой человек — передать (вне публичного репо!):
- [ ] доступ к GitHub-репо `ykvnkm/rescue-ai`;
- [ ] `scripts/security/out/authorized_key.json` (Yandex SA для KMS);
- [ ] `kms_key_id = <YOUR_KMS_KEY_ID>`;
- [ ] реквизиты managed-БД (host/port/user/pass);
- [ ] Yandex S3 access key + secret (artifacts);
- [ ] Gmail app-password для SMTP (`<your-email@example.com>`);
- [ ] Grafana admin-пароль;
- [ ] GHCR PAT (`GHCR_TOKEN`);
- [ ] доступ к панели домена `rescue-ai.ru` (правка A-записей);
- [ ] доступ к reg.ru (создание VPS);
- [ ] доступ к Yandex Cloud (на случай пересоздания KMS-ключа/SA).

---

## Известные грабли (быстрый справочник)

| Симптом | Причина | Фикс |
|---|---|---|
| `helm repo add hashicorp` → 403 | geo-block reg.ru | ставить из github static manifests (шаг 3) |
| helm/git к CDN зависает | сломан IPv6 reg.ru | отключить IPv6 (шаг 1.3) |
| api CrashLoop, не видит managed-БД | NetworkPolicy не пускает нестандартный порт БД | `extraEgressPorts` в `cloud.yaml` (есть для 18749; **сменить, если у новой БД другой порт**) |
| api 401 на `/ready` | выставлен `API_AUTH_TOKEN`, а `/ready` не в allowlist | в cloud токен пустой (открытое демо) |
| cert не выписывается | DNS A-запись не указывает на CP / не распространилась | проверить `dig`, дождаться, ClusterIssuer letsencrypt-prod |
| traceback в подах не виден | python буферизует stdout в контейнере | `PYTHONUNBUFFERED=1` |

---

## Примечание: источник образа Vault с yckms

**Подтверждено на первом прогоне (2026-06-05): вариант A.**
И чарт, и образ — публичные в `cr.yandex/yc-marketplace/...`, пуллятся
с reg.ru-ноды **без авторизации** (helm pull чарта и crictl pull образа
прошли напрямую). Docker-login в Yandex CR НЕ требуется.

- ✅ **вариант A (используем):**
  - чарт: `oci://cr.yandex/yc-marketplace/yandex-cloud/vault/chart/vault:0.30.0-3-yckms`
  - образ: `cr.yandex/crpsjg1coh47p81vh2lc/yandex-cloud/vault/vault:1.19.5.1_yckms`
- запасной вариант B (если cr.yandex однажды закроют): собрать из форка
  `github.com/yandex-cloud/vault` и запушить в наш GHCR
  `ghcr.io/ykvnkm/vault-yckms:<tag>`.
