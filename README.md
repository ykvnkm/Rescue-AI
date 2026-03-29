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
DB_DSN=postgresql://<user>:<password>@<host>:5432/<db>
ARTIFACTS_S3_ENDPOINT=https://storage.yandexcloud.net
ARTIFACTS_S3_REGION=ru-central1
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

DAG `rescue_batch_daily` сам находит миссии в S3 на текущую `ds`
(без ручного `BATCH_MISSION_ID`) и запускает pipeline для каждой найденной миссии.

### Ручной запуск batch без Airflow

```bash
# Запуск отдельной стадии пайплайна
uv run python -m rescue_ai.interfaces.cli.batch \
  --stage data \
  --mission-id demo_mission \
  --ds 2026-03-01

# Все стадии последовательно
for stage in data train validate inference; do
  uv run python -m rescue_ai.interfaces.cli.batch \
    --stage $stage \
    --mission-id demo_mission \
    --ds 2026-03-01 \
    --model-version yolov8n_baseline_multiscale
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
