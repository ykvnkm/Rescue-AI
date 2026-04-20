# ADR-0008: Kubernetes-деплой и управление секретами через Vault

- Статус: Принято
- Дата: 2026-04-20
- Авторы: Максим Яковенко

## Контекст

Текущий деплой Rescue-AI — Docker Compose ([docker-compose.yml](../../docker-compose.yml) для online, [infra/docker-compose.platform.yml](../../infra/docker-compose.platform.yml) для batch-платформы). Это адекватно для MVP и dev, но для дипломного проекта и для будущего роста системы требуется:

1. **Оркестрация на уровне Kubernetes** — автоматический рестарт упавших подов, rolling deployments, health-probes, horizontal scaling.
2. **Стандартный деплой, соответствующий промышленной практике** — один и тот же набор манифестов работает на dev-кластере, managed K8s в cloud и на edge-узле.
3. **Управление секретами через Vault** — текущий `.env` файл с DSN, S3 credentials, Telegram/SMTP токенами не подходит для prod.

Дополнительное требование из ADR-0007: деплой должен поддерживать три профиля (cloud, offline, hybrid), при этом профили должны отличаться только values-файлами, не манифестами.

## Решение

### 1. Набор K8s-артефактов

Используем **Helm** как стандарт для параметризованных деплоев:

```
infra/k8s/
  charts/
    rescue-ai-api/          # FastAPI + pilot + auto-mission routes
    rescue-ai-nav-engine/   # Navigation engine service
    rescue-ai-detection/    # YOLO + NanoDet inference
    rescue-ai-sync-worker/  # Hybrid outbox worker (опционально)
    rescue-ai-batch/        # Airflow DAG runner (опционально)
  values/
    cloud.yaml              # managed K8s, remote Postgres/S3
    offline.yaml            # k3s on station, local Postgres/MinIO
    hybrid.yaml             # k3s + sync-worker enabled
    dev.yaml                # kind/minikube для локальных тестов
  deps/
    # external dependencies, подключаются как subcharts:
    # - bitnami/postgresql
    # - bitnami/minio
    # - hashicorp/vault
```

Все чарты — вручную написанные (не генерируются), следуют канонической структуре (`templates/deployment.yaml`, `service.yaml`, `configmap.yaml`, `hpa.yaml`, `networkpolicy.yaml`, `servicemonitor.yaml` для P4).

### 2. Kubernetes-дистрибутивы по средам

| Среда | Дистрибутив | Обоснование |
|---|---|---|
| Локальная разработка | **Docker Compose** (как сейчас) | Быстрая итерация, не нужен K8s overhead. |
| Dev/CI интеграционные тесты | **kind** (Kubernetes in Docker) | Проверка Helm-чартов без реального кластера. |
| Cloud staging / production | **Managed Kubernetes** (Yandex Managed Kubernetes или аналог) | Стандартная промышленная практика, HA из коробки. |
| On-station (offline/hybrid) | **k3s** | Один бинарник 60 MB, полноценный K8s API, официальный CNCF-дистрибутив для edge. Единственный реалистичный вариант для однонодного автономного деплоя. |

**Ключевая инвариант**: Helm-чарты и манифесты **идентичны** для всех дистрибутивов. Отличаются только values-файлы. На защите это формулируется как *"единый deployment pipeline, поддерживающий edge и cloud развёртывания"*.

### 3. Vault для секретов

Vault выбран как стандартное решение (HashiCorp, широко используется в индустрии).

**Категории секретов**:
- `database/postgres-dsn` — credentials и DSN для Postgres (cloud или offline);
- `storage/s3` — access/secret key, endpoint;
- `rpi/mtls` — корневой CA, клиентский сертификат + ключ для связи с RPi;
- `telegram/bot` — токен бота для алертов (из P4);
- `smtp/credentials` — SMTP user/password для email-алертов (из P4);
- `vault-internal/unseal-keys` — ключи от самого Vault (только runbook, не в K8s).

**Инъекция в поды**: через **Vault Agent Injector** (sidecar pattern) — аннотация на поде `vault.hashicorp.com/agent-inject: "true"` + `vault.hashicorp.com/role: rescue-ai-api`, секреты рендерятся в файлы `/vault/secrets/*`. Приложение читает их как файлы (или через env, если используется `template`).

**Альтернатива рассмотрена**: External Secrets Operator. Отклонён — добавляет лишнюю CRD-зависимость, Vault Agent Injector проще.

**Fallback для dev**: приложение продолжает поддерживать чтение из `.env` файла. Если Vault-аннотации отсутствуют — читаются переменные окружения. Это сохраняет возможность локального запуска без Vault.

### 4. Ingress / сетевая топология

- **Cloud**: nginx-ingress-controller, TLS через cert-manager + Let's Encrypt, один хост `rescue-ai.<domain>`.
- **On-station (k3s)**: встроенный Traefik (k3s поставляет из коробки), TLS через самоподписанный сертификат (тот же механизм, что для mTLS из ADR-0007).
- **Dev**: Kind без ingress, port-forward.

### 5. CI/CD

- GitHub Actions workflow `deploy.yml` (существующий, для cloud):
  - build Docker images (online + batch) → push в registry (тот, что уже используется);
  - `helm upgrade --install` с `values/cloud.yaml` на staging-кластер;
  - manual approval gate для production.
- Для offline/hybrid: образы пушатся в тот же registry, деплой на станции делается вручную по runbook (`helm install --values values/offline.yaml` на k3s). Автоматизация полевого деплоя — вне scope этого ADR.

### 6. Миграция с Docker Compose

Docker Compose **не удаляется**. Остаётся как:
- Основной dev-workflow (`make up`).
- Fallback для простых демо (если на защите не будет доступа к K8s-кластеру).

Тот же образ, которым запускается compose, запускается и в K8s. Отличие — только в оркестрации.

## Рассмотренные альтернативы

### K8s-дистрибутив для on-station
1. **kubeadm** (vanilla K8s) — требует минимум трёх нод для HA, избыточен для однонодной станции.
2. **minikube** / **kind** — предназначены для разработки, не для продакшен-деплоя.
3. **OpenShift / Rancher** — платные или over-engineered для проекта уровня диплома.
4. **k3s (выбран)** — sweet spot: один бинарник, полный K8s API, production-ready, спонсируется CNCF.

### Secrets management
1. **`.env` файлы в git-crypt / sops** — отклонено, не интегрируется с K8s нативно.
2. **K8s native Secrets (без Vault)** — base64-encoded, не шифруются в etcd без дополнительной настройки. Не соответствует требованию "серьёзного проекта".
3. **AWS Secrets Manager / GCP Secret Manager** — привязка к одному облаку, не работает offline.
4. **Vault (выбран)** — open-source, работает в любой среде (cloud/edge), стандарт индустрии.

### Helm vs Kustomize
**Helm выбран** из-за параметризации values-файлами и готовых subchart'ов для Postgres/MinIO/Vault. Kustomize рассматривался как вторичный вариант, но требует больше boilerplate для тех же параметров.

## Последствия

### Плюсы

- Промышленный стандарт деплоя, легко объясняется на защите.
- Один набор Helm-чартов обслуживает три среды (cloud/offline/hybrid) + dev.
- Vault снимает вопросы безопасности секретов для prod.
- k3s позволяет говорить про edge-деплой в едином K8s-контексте, а не как про отдельную "station-сборку".
- CI/CD автоматизируется из коробки.

### Минусы

- Helm-чарты нужно написать и поддерживать (~5 чартов × ~500 строк yaml = ~2500 строк).
- Vault требует инициализации/unseal процедуры — отдельный runbook.
- k3s на ноутбуке GCS добавляет требования к ресурсам (~1 GB RAM для control-plane). Для современных ноутбуков приемлемо.
- Кривая обучения: работа с Helm/Vault требует освоения (если делегируется коллегам — см. P3 в плане).

## Дальнейшие шаги (порядок реализации в P3)

1. P3.1: Helm-чарт `rescue-ai-api` — самый простой, как reference. Проверить на `kind`. Values-файл `dev.yaml`.
2. P3.2: Чарты для остальных сервисов (`nav-engine`, `detection`, `sync-worker`, `batch`). Подключить subcharts `postgresql`, `minio`.
3. P3.3: Vault deployment через официальный Helm-чарт. Инициализация, unseal, политики для каждого сервиса (`rescue-ai-api`, `nav-engine`, и т.п.).
4. P3.4: Vault Agent Injector на всех подах. Миграция секретов из `.env` → Vault KV.
5. P3.5: Values-файлы для `offline.yaml` (local Postgres/MinIO) и `hybrid.yaml` (+ sync-worker).
6. P3.6: Обновить `.github/workflows/deploy.yml` на `helm upgrade --install`.
7. P3.7: Runbook'и `docs/operations/deploy-cloud.md`, `deploy-station.md`, `vault-unseal.md`.

## Ссылки

- [Текущий docker-compose (online)](../../docker-compose.yml)
- [Текущий docker-compose (batch platform)](../../infra/docker-compose.platform.yml)
- [Текущий CI/CD workflow](../../.github/workflows/deploy.yml)
- [Текущий .env.example](../../.env.example)
- k3s — https://k3s.io (официальный сайт)
- HashiCorp Vault — https://www.vaultproject.io
- Vault Agent Injector — https://developer.hashicorp.com/vault/docs/platform/k8s/injector
- ADR-0006 — Operator vs Automatic mode
- ADR-0007 — Автономный деплой и offline-синхронизация
