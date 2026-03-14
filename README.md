# Rescue AI

Rescue AI - сервис для запуска пилотной миссии по набору кадров, генерации alerts и расчета итогового mission report.

## Что важно про storage

У приложения есть два backend-режима для операционных данных:

- `memory` - `missions`, `alerts` и `frame_events` живут в памяти процесса.
- `postgres` - те же сущности живут в PostgreSQL, а схема создается миграциями Alembic.

Артефакты миссии не лежат в PostgreSQL:

- кадры, сохраненные для alert cards;
- mission report JSON;
- любые бинарные файлы.

За это отвечает только `ArtifactStorage` и переменные `ARTIFACTS_*`.
Переключение `APP_REPOSITORY_BACKEND` не меняет способ хранения артефактов.

## `episodes` и report

Postgres-таблица `episodes` - это read-model, а не отдельная доменная сущность.
Она хранит GT episodes, собранные из `frame_events`, и флаг `found_by_alert`.
Этот флаг считается по той же логике, что и `episodes_found` в mission report:
эпизод считается найденным, если в окно эпизода попал любой alert в пределах tolerance.

Review влияет на другие поля отчета:

- `alerts_confirmed`
- `alerts_rejected`
- `ttfc_sec`

## Быстрый старт

1. Скопируйте шаблон env:

```bash
cp .env.example .env
```

2. При необходимости задайте `MISSION_DIR` и настройки `ARTIFACTS_*`.

Структура mission directory:

```text
<mission>/
  images/
    frame_0001.jpg
    frame_0002.jpg
  annotations/
    mission.json
```

## Запуск без Postgres

Это режим по умолчанию.

В `.env` достаточно оставить:

```env
APP_REPOSITORY_BACKEND=memory
```

Запуск:

```bash
docker compose up --build
```

API будет доступен на `http://127.0.0.1:8000`.

## Запуск с Postgres

### Через Docker Compose

В `.env` задайте backend и параметры базы:

```env
APP_REPOSITORY_BACKEND=postgres
APP_POSTGRES_DB=rescue_ai
APP_POSTGRES_USER=rescue_ai
APP_POSTGRES_PASSWORD=change-me
APP_POSTGRES_PORT=5432
```

После этого поднимите тот же compose-файл, но с postgres profile:

```bash
docker compose --profile postgres up --build
```

Что произойдет:

- `postgres` поднимется как отдельный сервис в этом же `docker-compose.yml`;
- API внутри Docker будет ходить в базу по hostname `postgres`;
- перед стартом API дождется доступности БД и выполнит `alembic upgrade head`.

Для docker-сценария не нужно прописывать `host.docker.internal` или `127.0.0.1` в DSN контейнера.
Если вы все же используете `APP_POSTGRES_DSN` вместо `APP_POSTGRES_HOST/...`, указывайте в нем host `postgres`.

### Локальный запуск API вне Docker

Если API запускается не в контейнере, используйте локальный host:

```env
APP_REPOSITORY_BACKEND=postgres
APP_POSTGRES_HOST=127.0.0.1
APP_POSTGRES_PORT=5432
APP_POSTGRES_DB=rescue_ai
APP_POSTGRES_USER=rescue_ai
APP_POSTGRES_PASSWORD=change-me
```

Запуск:

```bash
uv run --extra dev --extra batch python -m services.api_gateway.run
```

## Миграции Alembic

Ручной запуск миграций:

```bash
make db-migrate
```

или:

```bash
uv run --extra dev --extra batch alembic upgrade head
```

Alembic читает либо `APP_POSTGRES_DSN`, либо набор переменных:

- `APP_POSTGRES_HOST`
- `APP_POSTGRES_PORT`
- `APP_POSTGRES_DB`
- `APP_POSTGRES_USER`
- `APP_POSTGRES_PASSWORD`

Проверить текущую ревизию:

```bash
uv run --extra dev --extra batch alembic current
```

## Тесты

Полный набор:

```bash
uv run --extra dev --extra batch pytest
```

Только postgres integration tests:

```bash
APP_TEST_POSTGRES_DSN=postgresql://<user>:<password>@127.0.0.1:5432/<db> uv run --extra dev --extra batch pytest tests/test_postgres_repositories.py -m integration
```

Postgres integration tests используют реальные Alembic migrations.
Для изоляции каждый тест поднимает собственную временную schema внутри указанной test database.

## Полезные команды

```bash
make up
make up-postgres
make down
make db-migrate
make test
```

## Batch и прочая документация

- [infra/README.md](infra/README.md)
- [docs/runbooks/postgres_backend.md](docs/runbooks/postgres_backend.md)
- [docs/runbooks/batch_operations.md](docs/runbooks/batch_operations.md)
- [docs/runbooks/batch_demo_playbook.md](docs/runbooks/batch_demo_playbook.md)
- [docs/architecture/batch_contour.md](docs/architecture/batch_contour.md)
- [docs/ml_system_design_doc.md](docs/ml_system_design_doc.md)
