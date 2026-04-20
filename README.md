# Rescue AI

Rescue AI — система для обнаружения людей на потоке кадров с БПЛА, обеспечивающая высокую точность детекции человека в реальном времени и применимая в реальных поисково-спасательных операциях, включая стихийные бедствия, горы и леса.

## Что умеет система

- Запускает миссию по потоку кадров с Raspberry Pi source-service (online) или по дате из S3-хранилища (batch).
- Выполняет детекцию людей на каждом кадре с помощью YOLOv8n.
- Формирует алерты присутствия человека (sliding window + quorum + cooldown + gap).
- Позволяет оператору подтвердить/отклонить алерты в UI (online) или автоматически ревьюит по GT (batch).
- Считает метрики миссии: `recall_event`, `ttfc_sec`, `fp_per_minute`, `episodes_total`, `episodes_found`, `false_alerts_total`.
- Сохраняет артефакты (отчеты, кадры) в удаленное S3-compatible хранилище.

Подробности по продуктовой и ML-логике: [ML System Design Doc](docs/ml_system_design_doc.md).

## Архитектура

```text
rescue_ai/
├── config.py               # предоставляет интерфейсы ко всем переменным окружения
│
├── domain/                 # бизнес-логика: что такое миссия, алерт, детекция, кадр
│                             правила предметной области и интерфейсы (порты),
│                             которые реализуют внешние слои
│
├── application/            # сценарии использования (use cases): как проходит миссия,
│                             как считаются метрики, стадии ML-пайплайна
│
├── infrastructure/         # интеграции с внешним миром
│                             детекция через YOLO, хранение в S3, работа с Postgres,
│                             чтение кадров, загрузка YAML-конфигов
│
└── interfaces/             # точки входа в приложение
    ├── api/                  REST API на FastAPI + UI оператора
    └── cli/                  CLI для batch-пайплайна и запуска API-сервера

configs/                    # YAML-контракт детекции и алертинга
docs/                       # документация, ML System Design Doc, runbook'и
infra/                      # Airflow DAG, docker-compose, инициализация Postgres
scripts/                    # вспомогательные скрипты
tests/
├── architecture/           # автотесты: проверяют, что слои не нарушают границы
└── test_*.py               # unit- и smoke-тесты
```

## Запуск online-сервиса (Docker)

### Требования

- Docker Desktop / Docker Engine
- Свободный порт `8000`

### Шаги запуска

1. Создайте `.env` из шаблона:

```bash
cp .env.example .env
```

2. Настройте подключение к БД, S3 и Raspberry Pi source-service:

```env
DB_DSN=postgresql:...

ARTIFACTS_S3_ENDPOINT=...
ARTIFACTS_S3_REGION=...
ARTIFACTS_S3_ACCESS_KEY_ID=...
ARTIFACTS_S3_SECRET_ACCESS_KEY=...
ARTIFACTS_S3_BUCKET=...

RPI_BASE_URL=http://<rpi-host>:<port>
RPI_MISSIONS_DIR=/home/<user>/missions
RPI_RTSP_PORT=<rtsp-port>
```

3. Поднимите сервис:

```bash
docker compose up --build
```

4. Проверьте health/readiness:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
curl http://127.0.0.1:8000/rpi/status
# {"status":"ok"}
```

### Сценарий работы в UI

1. Откройте UI: `http://127.0.0.1:8000/pilot`
2. Укажите `rpi_mission_id` (имя миссии на Raspberry Pi).
3. Нажмите **«Начать миссию»**.
4. Подтверждайте/отклоняйте алерты.
5. Нажмите **«Закончить миссию»** → **«Отчет по миссии»**.

## Batch-сервис (Airflow)

Batch-сервис запускается как отдельный Docker-контейнер через Airflow DockerOperator.

### Запуск Airflow-контура

```bash
cd infra
cp platform.env.example platform.env
# Заполните обязательные поля в platform.env (DSN, S3, Airflow)

# Запустите платформу
docker compose -f docker-compose.platform.yml up -d
```

Airflow UI: `http://localhost:8080`

DAG `rescue_batch_pipeline` сам находит миссии в S3 на текущую `ds`
и запускает пайплайн для каждой найденной миссии.

### Ручной запуск batch без Airflow

```bash
# Запуск отдельной стадии пайплайна
uv run python -m rescue_ai.interfaces.cli.batch \
  --stage prepare_dataset \
  --ds 2026-03-01

# Все стадии последовательно
for stage in prepare_dataset evaluate_model publish_metrics; do
  uv run python -m rescue_ai.interfaces.cli.batch \
    --stage $stage \
    --ds 2026-03-01
done
```

Подробнее:
- [docs/runbooks/batch_operations.md](docs/runbooks/batch_operations.md) — эксплуатация batch.
- [docs/architecture/batch_contour.md](docs/architecture/batch_contour.md) — архитектурная схема.

## Локальная разработка

### Установка зависимостей

```bash
uv sync --extra dev --extra batch
```

### Команды

```bash
make format    # форматирование (black + isort)
make lint      # проверка (black, isort, flake8, mypy, pylint, DAG syntax)
make test      # unit/smoke + архитектурные тесты с coverage >= 70%
make ci        # полный CI (lint + test)
```

### Запуск без Docker

```bash
uv run python -m rescue_ai.interfaces.cli.online
```

## CI/CD

- [`.github/workflows/ci.yml`](.github/workflows/ci.yml) — GitHub Actions: линтеры, типизация, unit/smoke-тесты с coverage >= 70%, архитектурные границы.
- [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml) — push-based deploy online + Airflow (DAG) на удаленный сервер через GHCR/SSH.
- Quality gate требует прохождения двух джобов: `lint`, `test`.

## Roadmap: слияние с diplom-prod и автономный деплой

Текущая версия поддерживает только **операторский режим** (ручное подтверждение алертов). В работе — слияние с [diplom-prod](https://github.com/ykvnkm/diplom-prod), где реализован **автоматический режим** с навигацией, RTSP-потоком и дополнительным детектором NanoDet. Целевая архитектура — единый codebase, поддерживающий оба режима, с возможностью полностью автономной работы на полевой станции.

### Фазы

| Фаза | Scope | Статус |
|---|---|---|
| **P0** | Архитектурные решения (3 ADR), план слияния, подготовка веток | в работе |
| **P1** | Слияние автоматического режима: domain-слой, навигация, video-ingest, NanoDet, use-case, API/CLI/UI | запланировано |
| **P2** | Автономный деплой: профили cloud/offline/hybrid, outbox sync, mTLS до RPi | запланировано (параллельно с P1.4+) |
| **P3** | Kubernetes-деплой (Helm + k3s + managed K8s) и Vault для секретов | запланировано (делегируется) |
| **P4** | Prometheus + Grafana + Alertmanager, ML drift (PSI/CSI) | запланировано (делегируется) |

### Архитектурные решения

- [ADR-0006: Operator vs Automatic mode](docs/adr/ADR-0006-operator-vs-automatic-mode.md) — гибридная модель с `Mission.mode` и таблицами-спутниками.
- [ADR-0007: Автономный деплой и offline-синхронизация](docs/adr/ADR-0007-autonomous-deployment-and-offline-sync.md) — три профиля деплоя, transactional outbox, mTLS до Raspberry Pi.
- [ADR-0008: Kubernetes и управление секретами](docs/adr/ADR-0008-kubernetes-and-secrets.md) — Helm + k3s (station) + managed K8s (cloud) + Vault.

### Работа ведётся в ветке

`feat/auto-mode-merge` — долгоживущая feature-ветка, от которой ответвляются маленькие PR по каждой подфазе.
