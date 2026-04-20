# ADR-0007: Автономный деплой, offline-работа и синхронизация артефактов

- Статус: Принято
- Дата: 2026-04-20
- Авторы: Максим Яковенко

## Контекст

Rescue-AI — наземная станция управления поисковым БПЛА. Целевой сценарий эксплуатации:

- Работа **без интернета** (полевые условия, удалённые районы).
- Работа **без GPS** (основное назначение системы — поиск людей с дрона в условиях отсутствия спутниковой навигации; траектория вычисляется из видеопотока).
- Связь с Raspberry Pi (edge-узел на БПЛА или на борту) **без использования публичных туннелей и сторонних облачных сервисов** (в поле нет белых IP, доменов, управляемой PKI).

Текущее состояние:
- Сервис деплоится на удалённый сервер через `docker-compose.yml`, использует remote Postgres, Yandex Cloud S3, cloudflared-туннель до RPi.
- Полностью offline-сценарий не поддерживается: Postgres и S3 — только remote, коннектора к локальным хранилищам нет.
- Связь с RPi — обычный HTTP без шифрования и аутентификации (см. `rescue_ai/infrastructure/rpi_client.py`).

Требования:
1. Один и тот же код должен запускаться в трёх режимах: **cloud** (как сейчас), **offline** (всё локально на станции), **hybrid** (локальная первичная запись + фоновая синхронизация в cloud при появлении связи).
2. Автономность не должна требовать костылей или отдельных веток кода.
3. Канал до RPi должен быть защищён (шифрование + взаимная аутентификация) без внешних зависимостей.

## Решение

### 1. Три профиля деплоя, один codebase

В `rescue_ai/config.py` вводится `DeploymentSettings.mode: Literal["cloud", "offline", "hybrid"]`. Профили различаются только **конфигурацией DSN/endpoint**, код приложения идентичен:

| Профиль | Postgres | S3 | Назначение |
|---|---|---|---|
| `cloud` | remote (Yandex Cloud / аналог) | Yandex Cloud S3 | Текущий серверный деплой, staging, демо |
| `offline` | local Postgres (контейнер на станции) | local MinIO (контейнер на станции) | Полевая эксплуатация без связи |
| `hybrid` | local Postgres + outbox | local MinIO + outbox | Полевая эксплуатация с периодическим появлением связи |

Все три профиля используют **один и тот же** набор Helm-чартов (см. ADR-0008), отличие — в values-файле.

### 2. Offline-стек

На наземной станции поднимается тот же набор сервисов, что и в cloud, но все зависимости локальные:

- **Postgres** (официальный образ 15, один контейнер/под, persistent volume).
- **MinIO** (S3-совместимое хранилище, один контейнер/под, persistent volume).
- **Rescue-AI API** (существующий образ).
- **NavigationEngine** (новый, из P1).
- **Detection service** (YOLO + NanoDet).

Модели предзагружаются в образ (build-time), чтобы не требовался интернет на первом старте. Это расширение существующего `runtime/models/` кеша.

### 3. Offline-first с transactional outbox (вариант `hybrid`)

Реализуем классический **Transactional Outbox Pattern** (Ричардсон, "Microservices Patterns"):

- Все бизнес-операции (создание миссии, запись кадра, алерта, траектории) идут через обёрточные репозитории (`OfflineFirstMissionRepository` и т.п.).
- Обёртка выполняет запись в **локальный** Postgres и одновременно в той же транзакции ставит запись в таблицу `replication_outbox(id, entity_type, entity_id, operation, payload_json, local_path, s3_bucket, s3_key, status, idempotency_key, created_at, updated_at)`.
- Отдельный процесс `sync-worker` периодически:
  1. Забирает pending-записи батчами.
  2. Для S3-артефактов: заливает файл в remote S3 и помечает запись `synced`.
  3. Для БД-записей: отправляет UPSERT в remote Postgres по `idempotency_key`, ON CONFLICT DO UPDATE.
  4. На ошибке: инкрементирует `attempts`, оставляет `pending`.
  5. Stuck-записи (processing > timeout) переводятся обратно в pending.
- Идемпотентность: естественный ключ (mission_id, frame_id, alert_id) + хеш операции.

**Базовый скелет уже был реализован в коммите `66ba4e9d`** (sync_worker.py, sync_outbox_repository.py, offline_first_repositories.py, 437 строк тестов). Переиспользуем идеи, переписываем в рамках текущей архитектуры и интегрируем в новые `Mission.mode`/trajectory-потоки.

### 4. Защищённый канал до Raspberry Pi: mTLS с самоподписанным CA

Требование — без публичного туннеля, без облачных сервисов, без белых IP. Выбран **mTLS (mutual TLS)**:

- Один раз генерируется корневой CA (`station-root-ca.crt`/`.key`) локально скриптом `scripts/security/gen_ca.sh`.
- CA подписывает два сертификата: `gcs-client.crt` (для ноутбука/станции) и `rpi-server.crt` (для Raspberry Pi).
- `rpi_source_service` на Pi поднимается с TLS (`ssl_certfile`/`ssl_keyfile` + `ssl_ca_certs` + `ssl_cert_reqs=CERT_REQUIRED`).
- `rpi_client.py` в rescue-ai использует `httpx.Client(verify=ca_path, cert=(client_crt, client_key))`.
- Флаг `security.tls_mode: Literal["off", "mtls"]`; `off` разрешён только в dev-среде и отклоняется рантаймом при `deployment.mode != "cloud"` + staging.
- Хранение ключей: файлы на диске с `chmod 600`; в k8s-профиле — как Secret из Vault (см. ADR-0008).

### 5. Cloud-деплой не меняется

Текущая схема (cloudflared туннель до RPi, Yandex Cloud S3, managed Postgres) **остаётся как есть**. mTLS для RPi добавляется как дополнительный слой поверх туннеля — это не ломает работу, только усиливает безопасность.

## Рассмотренные альтернативы

### Канал до RPi
1. **WireGuard point-to-point** — VPN-туннель между станцией и Pi. Плюс: прозрачен для приложения. Минус: зависит от настройки сетевого интерфейса, на защите объяснять как "стандарт проекта" сложнее.
2. **Tailscale / Headscale** — managed VPN. **Отклонён** — требует внешнего сервиса, противоречит требованию автономности.
3. **Публичный туннель (cloudflared/ngrok)** — **отклонён** для offline-профиля, сохраняется только в cloud.
4. **mTLS (выбран)** — классическая PKI-схема, объясняется одним предложением на защите, минимальные правки в коде (`httpx` уже поддерживает).

### Offline-синхронизация
1. **Только переключение endpoint'ов** (D5-a из обсуждения) — проще, но нет гарантии eventual consistency: если сетевое соединение появилось и пропало, часть данных может быть не залита.
2. **Outbox + sync-worker** (выбран) — гарантирует at-least-once доставку, идемпотентность через естественный ключ, устойчивость к сбоям. Скелет уже был реализован, переиспользуем.
3. **Event sourcing / Kafka** — избыточно для проекта, добавляет новые инфра-сущности.

## Последствия

### Плюсы

- Один codebase, три профиля деплоя через конфиг. Нет развилок в коде типа `if offline: ...`.
- Offline-режим работает без интернета с первого старта (модели в образе, локальные Postgres/MinIO).
- Hybrid-режим даёт at-least-once синхронизацию в cloud с идемпотентностью.
- mTLS — без внешних зависимостей, стандартная криптография.
- Cloud-деплой никак не ломается — только добавляется mTLS-слой для RPi.

### Минусы

- Offline-образ крупнее (~2-3 GB из-за весов моделей). Приемлемо для on-station деплоя.
- Outbox добавляет 3 новые таблицы и один процесс (`sync-worker`) в hybrid-профиле. Отдельная зона ответственности, тестируется отдельно.
- Сертификаты mTLS имеют срок действия → нужен процесс ротации (на уровне диплома — one-off, для продакшена — документация в runbook).
- Persistent volume для локального Postgres/MinIO — эксплуатационный риск (бэкапы делаются только в hybrid-режиме при синхронизации).

## Дальнейшие шаги (порядок реализации в P2)

1. P2.1: Ввести `DeploymentSettings.mode` в `rescue_ai/config.py`, сделать DSN/endpoint опциональными.
2. P2.2: `infra/offline/docker-compose.offline.yml` — stack Postgres + MinIO + API + Nav + Detection. Отдельный `Dockerfile.offline` с предзагруженными моделями.
3. P2.3 (hybrid): таблица `replication_outbox`, порт `SyncOutbox`, `PostgresSyncOutboxRepository`, `SyncWorker`, обёрточные `OfflineFirst*Repository`. Тесты at-least-once + идемпотентности.
4. P2.4: `scripts/security/gen_ca.sh`, `gen_rpi_cert.sh`, `gen_client_cert.sh`. Обновить `rpi_client.py` на поддержку mTLS. Обновить `rpi_source_service.py` в diplom-prod (после порта в P1) на TLS listen.
5. В документации: runbook "Развёртывание на полевой станции" в `docs/operations/`.

## Ссылки

- [Текущий RPi-клиент](../../rescue_ai/infrastructure/rpi_client.py)
- [Текущая конфигурация](../../rescue_ai/config.py)
- [Существующий docker-compose (cloud)](../../docker-compose.yml)
- Transactional Outbox Pattern — Chris Richardson, "Microservices Patterns", ch. 3
- [Предыдущая попытка sync-worker (reference)](https://github.com/ykvnkm/Rescue-AI/commit/66ba4e9d92aea9140f6fdf8e334f64be0310c0c2)
- ADR-0006 — Operator vs Automatic mode
- ADR-0008 — Kubernetes и управление секретами
