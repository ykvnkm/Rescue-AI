# PostgreSQL backend for API Gateway

Этот runbook описывает, как включить persistent backend для `missions`,
`alerts`, `frame_events` и `episodes`, не меняя текущую логику хранения
артефактов.

## Что хранится в PostgreSQL

- `missions`
- `alerts`
- `frame_events`
- `episodes`

Важно:

- кадры, изображения alert-ов и JSON-отчеты не пишутся в PostgreSQL;
- бинарные артефакты продолжают идти через `ArtifactStorage`;
- `ARTIFACTS_MODE=s3` с автоматическим fallback на local остается без изменений.

## 1. Установить зависимости

Для локальной разработки и миграций нужен dev-окружение с postgres-драйвером:

```bash
uv sync --extra dev --extra batch
```

Через `make`:

```bash
make install
```

## 2. Поднять локальный PostgreSQL

В репозитории есть минимальный compose-файл:

```bash
docker compose -f docker-compose.postgres.yml up -d
```

По умолчанию он поднимает базу:

- host: `127.0.0.1`
- port: `5432`
- db: `rescue_ai`
- user: `rescue_ai`
- password: `rescue_ai_dev`

Остановить контейнер:

```bash
docker compose -f docker-compose.postgres.yml down
```

## 3. Настроить env

Добавьте или обновите переменные окружения:

```env
APP_REPOSITORY_BACKEND=postgres
APP_POSTGRES_DSN=postgresql://rescue_ai:rescue_ai_dev@127.0.0.1:5432/rescue_ai
```

Настройки артефактов остаются отдельными:

```env
ARTIFACTS_MODE=s3
ARTIFACTS_S3_ENDPOINT=...
ARTIFACTS_S3_REGION=...
ARTIFACTS_S3_ACCESS_KEY_ID=...
ARTIFACTS_S3_SECRET_ACCESS_KEY=...
ARTIFACTS_S3_BUCKET=...
```

Если S3-ключи не заданы, сервис по-прежнему сохраняет артефакты в
`runtime/artifacts`.

## 4. Применить миграции

```bash
APP_POSTGRES_DSN=postgresql://rescue_ai:rescue_ai_dev@127.0.0.1:5432/rescue_ai \
uv run --extra dev --extra batch alembic upgrade head
```

Проверить текущую ревизию:

```bash
APP_POSTGRES_DSN=postgresql://rescue_ai:rescue_ai_dev@127.0.0.1:5432/rescue_ai \
uv run --extra dev --extra batch alembic current
```

## 5. Запустить API с postgres backend

Через текущий `docker compose`/`.env`:

```env
APP_REPOSITORY_BACKEND=postgres
APP_POSTGRES_DSN=postgresql://rescue_ai:rescue_ai_dev@host.docker.internal:5432/rescue_ai
```

После этого:

```bash
docker compose up --build
```

Локально без Docker:

```bash
uv run --extra dev --extra batch python -m uvicorn services.api_gateway.app:app --host 0.0.0.0 --port 8000
```

## 6. Запустить тесты

Unit + integration:

```bash
APP_TEST_POSTGRES_DSN=postgresql://rescue_ai:rescue_ai_dev@127.0.0.1:5432/rescue_ai \
BATCH_TEST_POSTGRES_DSN=postgresql://rescue_ai:rescue_ai_dev@127.0.0.1:5432/rescue_ai \
uv run --extra dev --extra batch pytest
```

Только postgres integration:

```bash
APP_TEST_POSTGRES_DSN=postgresql://rescue_ai:rescue_ai_dev@127.0.0.1:5432/rescue_ai \
uv run --extra dev --extra batch pytest tests/test_postgres_repositories.py -m integration
```

## 7. Откатиться на memory backend

Чтобы вернуться к текущему in-memory режиму:

```env
APP_REPOSITORY_BACKEND=memory
```

`APP_POSTGRES_DSN` можно оставить незаданным.

Дополнительных изменений для `ARTIFACTS_MODE` не требуется.
