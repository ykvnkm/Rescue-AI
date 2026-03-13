# Rescue AI

Rescue AI — система для обнаружения людей на потоке кадров с БПЛА без облачной поддержки, обеспечивающая высокую точность детекции человека в реальном времени и применимая в реальных поисково-спасательных операциях, включая стихийные бедствия, горы и леса.

Текущая версия проекта ориентирована на базовый сценарий: загрузить набор кадров, запустить поток, получить алерты и итоговый отчет по миссии. В рамках MVP планируется перенос артефактов на удаленное хранилище и полный переход на чтение потока с периферийного устройства через RTSP/UDP-протокол.

## Что умеет сервис

- Запускает миссию по потоку кадров.
- Выполняет детекцию людей на каждом кадре потока.
- Формирует алерты присутствия человека и дает оператору подтвердить/отклонить их в UI.
- Считает ключевые метрики миссии: количество эпизодов реального присутствия человека в кадре `episodes_total`, найденных эпизодов `episodes_found`, полнота по эпизодам `recall_event`, время до первой подтвержденной детекции `ttfc_sec`, число ложных алертов `false_alerts_total`, ложных алертов в минуту `fp_per_minute`.

Подробности по продуктовой и ML-логике: [ML System Design Doc](docs/ml_system_design_doc.md).

## Структура проекта

```text
config.py                # единая точка доступа к переменным окружения
configs/                 # YAML-контракт детекции и алертинга
docs/                    # документация: архитектура, runbook'и, ML SDD
infra/                   # инфраструктура для батчевого сервиса (Airflow, Postgres, monitoring)
  airflow/dags/          # DAG-и Airflow
  docker-compose.platform.yml  # compose для запуска batch-платформы
libs/
  core/
    domain/              # сущности миссии/алертов
    application/         # бизнес-правила, расчет метрик, сервис миссии
  batch/
    domain/              # модели batch-запуска и результатов
    application/         # batch use-case (идемпотентный прогон миссии)
    infrastructure/      # адаптеры: S3/local artifact store, status store, runtime
  infra/postgres/        # postgres-адаптеры репозиториев
services/
  api_gateway/
    presentation/        # HTTP-роуты и UI оператора
    infrastructure/      # адаптеры хранилищ и интеграций API
    dependencies.py      # сборка контейнера зависимостей приложения
  detection_service/
    domain/              # модели и интерфейсы детекции
    application/         # оркестрация обработки потока кадров
    infrastructure/      # интеграции с YAML, YOLO и HTTP
    presentation/        # API для запуска и контроля стрима
  batch_runner/
    main.py              # основной файл запуска батч-обработки
tests/
  architecture/          # тесты границ слоев (import boundaries)
  test_*.py              # unit/integration/e2e тесты сервисов
.github/workflows/       # сценарии автоматических проверок и запусков в GHA
```

## Запуск через Docker

### Требования

- Docker Desktop / Docker Engine
- Свободный порт `8000`

### Подготовка данных миссии

Нужна локальная папка миссии со структурой:

```text
<mission>/
  images/
    frame_0001.jpg
    frame_0002.jpg
    ...
  annotations/
    *.json   # COCO annotations
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

Настройте, куда сохранять артефакты миссии (кадры и отчеты):

```env
ARTIFACTS_MODE=s3
```

Как это работает:
- по умолчанию используется режим `s3`;
- если ключи `ARTIFACTS_S3_ACCESS_KEY_ID` и `ARTIFACTS_S3_SECRET_ACCESS_KEY` не заданы, сервис автоматически пишет артефакты локально;
- если ключи заданы, но не хватает остальных параметров S3, сервис завершится с понятной ошибкой на старте.

Что обязательно заполнить для записи в S3-бакет:

```env
ARTIFACTS_S3_ENDPOINT=...
ARTIFACTS_S3_REGION=...
ARTIFACTS_S3_ACCESS_KEY_ID=...
ARTIFACTS_S3_SECRET_ACCESS_KEY=...
ARTIFACTS_S3_BUCKET=...
ARTIFACTS_S3_STRICT=true
```

3. Поднимите сервис:

```bash
docker compose up --build
```

4. Проверьте health:

```bash
curl http://127.0.0.1:8000/health
```

Ожидаемый ответ:

```json
{"status":"ok"}
```

## Как воспроизвести базовый сценарий в UI

1. Откройте UI: `http://127.0.0.1:8000/`
2. В поле **«Путь к папке с кадрами»** укажите:

```text
/data/mission/images
```

3. Нажмите **«Начать миссию»**.
4. В процессе обработки подтверждайте/отклоняйте алерты.
5. После окончания нажмите **«Закончить миссию»** и **«Отчет по миссии»**.
6. В таблице отчета получите рассчитанные метрики миссии.

## Остановка и повторный запуск

- Остановить сервис:

```bash
docker compose down
```

- Повторно запустить с теми же параметрами:

```bash
docker compose up --build
```

- Если нужно прогнать другую миссию:
1. Измените `MISSION_DIR` в `.env` на новый путь.
2. Перезапустите контейнер: `docker compose down && docker compose up --build`.

## Батчевый сервис (Airflow)

- [infra/README.md](infra/README.md) — как поднять Airflow-контур и что именно происходит в DAG.
- [docs/runbooks/batch_operations.md](docs/runbooks/batch_operations.md) — эксплуатация: safe rerun, диагностика `failed/partial`, проверка статусов.
- [docs/runbooks/batch_demo_playbook.md](docs/runbooks/batch_demo_playbook.md) — сценарий демонстрации батча на реальных данных.
- [docs/runbooks/postgres_backend.md](docs/runbooks/postgres_backend.md) — как включить PostgreSQL backend для `missions`/`alerts`/`frame_events`/`episodes`, применить миграции и откатиться на memory.
- [docs/architecture/batch_contour.md](docs/architecture/batch_contour.md) — архитектурная схема batch-контура.
- [docs/batch_evidence_pack.md](docs/batch_evidence_pack.md) — что собрать для защиты/сдачи.

- Airflow оркестрирует запуск batch-runner контейнера через `DockerOperator`.
- Бизнес-логика находится в коде проекта (`libs/batch/application`), а DAG управляет расписанием и backfill.
- Идемпотентность обеспечивается run-key и статусами, чтобы повторный запуск на тот же ключ не создавал дублей без `--force`.

## PostgreSQL backend

Для persistent storage миссий и alert-ов используйте:

```env
APP_REPOSITORY_BACKEND=postgres
APP_POSTGRES_DSN=postgresql://rescue_ai:rescue_ai_dev@127.0.0.1:5432/rescue_ai
```

Схема БД применяется через Alembic:

```bash
uv run --extra dev --extra batch alembic upgrade head
```

Локальный Postgres можно поднять так:

```bash
docker compose -f docker-compose.postgres.yml up -d
```

Подробный пошаговый runbook: [docs/runbooks/postgres_backend.md](docs/runbooks/postgres_backend.md).

## CI/CD каркас

- [`.github/workflows/ci.yml`](.github/workflows/ci.yml) — основной CI: линтеры, типизация, тесты с coverage-порогом, архитектурные проверки.
- [`.github/workflows/infra-ci.yml`](.github/workflows/infra-ci.yml) — проверка инфраструктурного контура: валидность compose и DAG-артефактов.
- [`.github/workflows/batch-e2e.yml`](.github/workflows/batch-e2e.yml) — e2e backfill сценарий для batch-контура (по расписанию и вручную).
