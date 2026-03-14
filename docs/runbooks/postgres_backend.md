# PostgreSQL backend for API Gateway

Этот runbook описывает только operational storage API Gateway:

- `missions`
- `alerts`
- `frame_events`
- `episodes`

Артефакты миссии по-прежнему идут через `ArtifactStorage`:

- кадры и image URIs не переносятся в PostgreSQL;
- mission report JSON не хранится в PostgreSQL;
- `ARTIFACTS_*` отвечают только за artifact storage.

## Env

Минимальный набор переменных для postgres backend:

```env
APP_REPOSITORY_BACKEND=postgres
APP_POSTGRES_HOST=127.0.0.1
APP_POSTGRES_PORT=5432
APP_POSTGRES_DB=rescue_ai
APP_POSTGRES_USER=rescue_ai
APP_POSTGRES_PASSWORD=change-me
```

Если удобнее, можно задать единый DSN:

```env
APP_POSTGRES_DSN=postgresql://<user>:<password>@127.0.0.1:5432/<db>
```

`APP_POSTGRES_DSN` имеет приоритет над `APP_POSTGRES_HOST/PORT/DB/USER/PASSWORD`.

## Docker Compose

В проекте используется один `docker-compose.yml`.

Запуск API без Postgres:

```bash
docker compose up --build
```

Запуск API вместе с Postgres:

```bash
docker compose --profile postgres up --build
```

В docker-сценарии API контейнер автоматически использует hostname `postgres`.
Отдельно прописывать `host.docker.internal` не нужно.
Если задаете `APP_POSTGRES_DSN`, host в нем тоже должен быть `postgres`.

При `APP_REPOSITORY_BACKEND=postgres` контейнер API:

1. дожидается доступности БД;
2. выполняет `alembic upgrade head`;
3. запускает uvicorn.

## Локальный запуск без Docker

```bash
uv run --extra dev --extra batch python -m services.api_gateway.run
```

В этом сценарии используйте `APP_POSTGRES_HOST=127.0.0.1`, если Postgres поднят локально.

## Миграции

Применить миграции вручную:

```bash
make db-migrate
```

или:

```bash
uv run --extra dev --extra batch alembic upgrade head
```

Проверить текущую ревизию:

```bash
uv run --extra dev --extra batch alembic current
```

## `episodes` projection

`episodes` не является отдельной core-сущностью.
Это read-model, собранная из `frame_events`.

Поле `found_by_alert` синхронизировано с логикой mission report:

- `episodes_found` в отчете считает эпизод найденным, если в окно эпизода попал любой alert;
- `episodes.found_by_alert` использует ту же семантику;
- review outcomes остаются в `alerts_*` и `ttfc_sec`.

## Тесты

Полный suite:

```bash
uv run --extra dev --extra batch pytest
```

Только postgres integration:

```bash
APP_TEST_POSTGRES_DSN=postgresql://<user>:<password>@127.0.0.1:5432/<db> uv run --extra dev --extra batch pytest tests/test_postgres_repositories.py -m integration
```

Integration tests не создают таблицы вручную.
Они поднимают временную schema и применяют к ней реальные Alembic migrations (`upgrade head`).

## Возврат в memory

```env
APP_REPOSITORY_BACKEND=memory
```

Остальные Postgres переменные можно оставить незаполненными.
