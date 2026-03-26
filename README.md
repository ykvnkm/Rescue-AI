# Rescue AI

Rescue AI — система для обнаружения людей на потоке кадров с БПЛА, обеспечивающая высокую точность детекции человека в реальном времени и применимая в реальных поисково-спасательных операциях, включая стихийные бедствия, горы и леса.

## Что умеет система

- Запускает миссию по потоку кадров (online) или по дате из файловой структуры (batch).
- Выполняет детекцию людей на каждом кадре с помощью YOLOv8n.
- Формирует алерты присутствия человека (sliding window + quorum + cooldown + gap).
- Позволяет оператору подтвердить/отклонить алерты в UI (online) или автоматически ревьюит по GT (batch).
- Считает метрики миссии: `recall_event`, `ttfc_sec`, `fp_per_minute`, `episodes_total`, `episodes_found`, `false_alerts_total`.
- Сохраняет артефакты (отчеты, кадры) в S3 (Yandex Cloud Storage) или локально.

Подробности по продуктовой и ML-логике: [ML System Design Doc](docs/ml_system_design_doc.md).

## Архитектура

```text
rescue_ai/
├── config.py               # все переменные окружения читаются здесь и только здесь
│
├── domain/                 # бизнес-логика: что такое миссия, алерт, детекция, кадр
│                             правила предметной области и интерфейсы (порты),
│                             которые реализуют внешние слои
│
├── application/            # сценарии использования (use cases): как проходит миссия,
│                             как считаются метрики, batch-прогон, стадии ML-пайплайна
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
scripts/                    # вспомогательные скрипты (quality gate)
tests/
├── architecture/           # автотесты: проверяют, что слои не нарушают границы
└── test_*.py               # unit- и smoke-тесты
```

## Запуск online-сервиса (Docker)

### Требования

- Docker Desktop / Docker Engine
- Свободный порт `8000`

### Подготовка данных миссии

Нужна локальная папка миссии со структурой:

```text
<mission>/
  images/
    <mission>_000001.jpg
    <mission>_000002.jpg
    ...
  annotations/
    *.json   # COCO annotations (опционально)
```

### Шаги запуска

1. Создайте `.env` из шаблона:

```bash
cp .env.example .env
```

2. В `.env` задайте путь к папке миссии на вашем устройстве:

```env
MISSION_DIR=/abs/path/to/mission
```

3. Настройте хранилище артефактов:

```env
# По умолчанию — S3. Если ключи не заданы, пишет локально.
ARTIFACTS_S3_ENDPOINT=https://storage.yandexcloud.net
ARTIFACTS_S3_REGION=ru-central1
ARTIFACTS_S3_ACCESS_KEY_ID=...
ARTIFACTS_S3_SECRET_ACCESS_KEY=...
ARTIFACTS_S3_BUCKET=...
```

4. Поднимите сервис:

```bash
docker compose up --build
```

5. Проверьте health:

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}
```

### Сценарий работы в UI

1. Откройте UI: `http://127.0.0.1:8000/`
2. Укажите путь к кадрам: `/data/mission/images`
3. Нажмите **«Начать миссию»**.
4. Подтверждайте/отклоняйте алерты.
5. Нажмите **«Закончить миссию»** → **«Отчет по миссии»**.

## Batch-сервис (Airflow)

Batch-сервис запускается как отдельный Docker-контейнер через Airflow DockerOperator.

### Запуск Airflow-контура

```bash
cd infra
cp platform.env.example platform.env
# Заполните обязательные поля в platform.env (пароли Postgres, Airflow)

# Соберите batch-образ
docker compose -f docker-compose.platform.yml --profile batch-build up batch-runner-image

# Запустите платформу
docker compose -f docker-compose.platform.yml up -d
```

Airflow UI: `http://localhost:8080`

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
make test      # unit/smoke тесты с coverage >= 70%
make test-arch # тесты архитектурных границ
make ci        # полный CI (lint + test + test-arch)
```

### Запуск без Docker

```bash
uv run python -m rescue_ai.interfaces.cli.online
```

## CI/CD

- [`.github/workflows/ci.yml`](.github/workflows/ci.yml) — GitHub Actions: линтеры, типизация, unit/smoke-тесты с coverage >= 70%, архитектурные границы.
- Quality gate требует прохождения всех трех джобов: `lint`, `test`, `architecture-boundaries`.
