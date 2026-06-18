# Rescue AI

Rescue AI — программный сервис обработки данных для системы обнаружения людей с БПЛА:
детекция человека в реальном времени, восстановление траектории полёта без внешней
навигации и преобразование детекций в сигналы оператору. Рассчитан на работу полностью
автономно, на маломощной наземной станции без интернета и видеокарты, и применим в
реальных поисково-спасательных операциях (горы, лес, зоны бедствий).

## Быстрый старт (Docker Compose)

```bash
cp .env.example .env          # заполнить локальные пароли (Postgres, MinIO, Grafana)
docker compose up -d --build
```

Поднимается полный стек из 11 сервисов: `postgres`, `minio`, `api`, `detection`,
`nav-engine`, `batch-exporter`, `airflow` (webserver + scheduler), `prometheus`,
`alertmanager`, `grafana`.

Проверка готовности:

```bash
curl http://localhost:8000/health   # {"status":"ok"}
curl http://localhost:8000/ready    # {"checks":{"database":true,"storage":true}}
```

- UI оператора: <http://localhost:8000/pilot>
- Grafana: <http://localhost:3000> · MinIO: <http://localhost:9001> · Airflow: <http://localhost:8080>

Остановка: `docker compose down` (с очисткой данных — `docker compose down -v`).

## Что умеет система

- **Источники кадров** — всеядность: живой RTSP-поток с борта (с запасным каналом MJPEG
  по HTTP), записанные видеофайлы, папки и архивы кадров, повторный прогон завершённых
  миссий из S3-хранилища.
- **Детекция людей** на каждом кадре моделью YOLOv8n; формат исполнения выбирается
  контрактом (NCNN как основной на CPU, PyTorch для стенда), модель подгружается из
  хранилища с проверкой контрольной суммы.
- **Восстановление траектории** полёта сервисом `nav-engine` (визуальная одометрия,
  режимы с маркером и без) — без GPS и внешней навигации.
- **Сигналы оператору** из покадровых детекций по эпизодам (скользящее окно + кворум +
  пауза между сигналами), удержание частоты ложных тревог ниже порога потери бдительности.
- **Два режима миссии**: операторский (ручное подтверждение/отклонение сигналов в UI) и
  автоматический (прогон записи без оператора, живые графики траектории по WebSocket).
- **Два профиля развёртывания** на одной кодовой базе: полевой (offline) и удалённый
  (cloud) — см. ниже.
- **Метрики миссии**: `recall_event`, `ttfc_sec`, `fp_per_minute`, `episodes_total`,
  `episodes_found`, `false_alerts_total`.
- **Эксплуатационный контур**: фоновая переоценка качества на исторических данных
  (Airflow), контроль дрейфа данных (PSI/CSI), мониторинг и оповещение
  (Prometheus + Grafana + Alertmanager).

Подробности по продуктовой и ML-логике: [ML System Design Doc](docs/ml_system_design_doc.md).

## Архитектура

Чистая архитектура: зависимости направлены внутрь, к домену; внешний мир подключается
через порты (`domain/ports.py`) и адаптеры. Сервисы делят общее доменное ядро, но
собираются в отдельные образы и масштабируются независимо.

```text
rescue_ai/
├── config.py               # доступ ко всем переменным окружения (Pydantic-Settings)
├── domain/                 # сущности, правила (политика сигналов, метрики), порты
├── application/            # сценарии: ход миссии, авто-сессии, стадии ML-пайплайна
├── infrastructure/         # адаптеры: YOLO (pt/ncnn), S3, Postgres, видео-источники,
│                             навигация, outbox-синхронизация, rpi-клиент
└── interfaces/             # точки входа: REST API + UI (api), HTTP-обёртки сервисов
                              (detection, nav-engine), CLI (online, batch), sync-worker

configs/                    # YAML-контракт детекции и алертинга
infra/
├── postgres/init/          # SQL-инициализация схемы
├── airflow/dags/           # DAG фоновой переоценки качества
└── k8s/                    # Helm-чарты сервисов, зонтичные чарты, values-профили,
                              vault (политики, values), observability
docs/                       # ML System Design Doc, ADR, runbook'и
tests/
├── architecture/           # автотесты границ слоёв
└── test_*.py               # unit / integration / smoke
```

Сервисы: `api` (управление миссией + UI + оркестрация), `detection`, `nav-engine`,
`sync-worker` (доставка outbox в удалённый контур, только offline), `batch-worker`
(стадии ночного прогона), `batch-exporter` (метрики батча в Prometheus).

## Профили развёртывания

Один и тот же код запускается в двух профилях, отличаются только values-файлы.

**Локальный (offline)** — полевая наземная станция без интернета: всё исполняется
локально, хранилища встроены в стек (Postgres + MinIO), связь с бортом по mTLS, при
появлении связи накопленные данные доставляются в центр через outbox-синхронизацию.

**Удалённый (cloud)** — центральный контур для разбора полётов, переоценки качества и
демонстраций: managed-Postgres, Yandex Object Storage, доступ снаружи через ingress с
TLS, автомасштабирование (HPA), несколько реплик с разнесением по узлам.

Оркестрация — Kubernetes (лёгкий дистрибутив **k3s**), пакетирование — **Helm**
(зонтичный чарт + подчарты сервисов, профили в `infra/k8s/values/`). Секреты — **Vault**
(Agent Injection, политики «одна роль — один путь», auto-unseal через KMS в облаке).
Наблюдаемость — отдельный чарт `rescue-ai-observability` (Prometheus + Alertmanager +
Grafana). Подробные пошаговые процедуры — в [docs/runbooks/](docs/runbooks/).

## Сценарий работы оператора (UI)

1. Откройте UI: <http://localhost:8000/pilot>
2. Проверьте индикатор связи с бортом, выберите источник (борт / видео / архив / S3).
3. **«Начать миссию»** → живая трансляция с подсветкой обнаружений и графики траектории.
4. Подтверждайте/отклоняйте сигналы.
5. **«Закончить миссию»** → **«Отчёт по миссии»**.

## Batch-сервис (Airflow)

DAG `rescue_batch_pipeline` (три стадии: `prepare_dataset → evaluate_model →
publish_metrics`) находит миссии в S3 на дату, прогоняет модель против доразметки и
публикует метрики качества и дрейфа. Запуск — по расписанию (`@daily` с catchup) или
вручную.

Ручной прогон стадии без Airflow:

```bash
uv run python -m rescue_ai.interfaces.cli.batch --stage prepare_dataset --ds 2026-03-01
```

Подробнее: [batch_operations.md](docs/runbooks/batch_operations.md),
[batch_contour.md](docs/architecture/batch_contour.md).

## Локальная разработка

```bash
uv sync --extra dev      # зависимости
make format              # black + isort
make lint                # black, isort, flake8, mypy, pylint, импорт-проверка DAG
make test                # unit / integration / архитектурные тесты, coverage >= 70%
make ci                  # lint + test + helm lint
```

Запуск API без Docker: `uv run python -m rescue_ai.interfaces.cli.online`.

## CI/CD

- [`ci.yml`](.github/workflows/ci.yml) — линтеры, типизация, импорт-проверка DAG,
  unit/integration-тесты на сервисном PostgreSQL с coverage ≥ 70%, quality-gate.
- [`k8s-lint.yml`](.github/workflows/k8s-lint.yml) — `helm lint` подчартов и
  `helm template` по обоим профилям с проверкой инвариантов.
- [`deploy.yml`](.github/workflows/deploy.yml) — сборка образов в GHCR и `helm upgrade`
  по SSH в кластер (приложение + контур наблюдаемости).

## Архитектурные решения (ADR)

- [ADR-0006](docs/adr/ADR-0006-operator-vs-automatic-mode.md) — операторский и
  автоматический режимы (гибридная модель с `Mission.mode` и таблицами-спутниками).
- [ADR-0007](docs/adr/ADR-0007-autonomous-deployment-and-offline-sync.md) — автономный
  деплой, профили offline/cloud, transactional outbox, mTLS до Raspberry Pi.
- [ADR-0008](docs/adr/ADR-0008-kubernetes-and-secrets.md) — Kubernetes (k3s) + Helm +
  Vault и управление секретами.
