# Rescue AI

Rescue AI — система для обнаружения людей на потоке кадров с БПЛА без облачной поддержки, обеспечивающая высокую точность детекции человека в реальном времени и применимая в реальных поисково-спасательных операциях, включая стихийные бедствия, горы и леса. Текущая версия проекта ориентирована на базовый сценарий: загрузить набор кадров, запустить поток, получить алерты и итоговый отчет по миссии.

В текущей ветке дополнительно реализованы: поддержка operational state в Postgres, хранение артефактов в S3 как целевой режим pilot/prod, отдельный batch-контур на Airflow и автоматический secret scanning в CI.

## Что умеет сервис

- Запускает миссию по потоку кадров.
- Выполняет детекцию людей на каждом кадре потока.
- Формирует алерты присутствия человека и дает оператору подтвердить/отклонить их в UI.
- Считает ключевые метрики миссии: количество эпизодов реального присутствия человека в кадре `episodes_total`, найденных эпизодов `episodes_found`, полнота по эпизодам `recall_event`, время до первой подтвержденной детекции `ttfc_sec`, число алертов `alerts_total`, подтвержденных алертов `alerts_confirmed`, отклоненных алертов `alerts_rejected`, ложных алертов в минуту `fp_per_minute`.
- Хранит operational state в `memory` или `postgres`, а артефакты — в отдельном storage-слое: `local` для dev/test и `S3` для pilot/prod.
- Поддерживает batch/backfill сценарии через Airflow + DockerOperator.

Подробности по продуктовой и ML-логике: [ML System Design Doc](docs/ml_system_design_doc.md).

## Структура проекта

```text
config.py                         # единая точка доступа к переменным окружения
configs/                          # YAML-контракты детекции, алертинга и метрик
db_migrations/                    # миграции Postgres

docs/                             # документация: архитектура, runbook'и, ML SDD
infra/                            # инфраструктура batch-контура (Airflow, monitoring, compose)
  docker-compose.platform.yml     # compose для запуска batch-платформы

libs/
  core/
    domain/                       # сущности миссии, алертов и отчетов
    application/                  # бизнес-правила, расчет метрик, сервис миссии
  batch/
    domain/                       # модели batch-запуска и результатов
    application/                  # batch use-case, идемпотентный прогон миссии
    infrastructure/               # status store, artifact store, runtime adapters
  infra/
    memory/                       # in-memory адаптеры репозиториев
    postgres/                     # postgres-адаптеры и соединение

services/
  api_gateway/
    presentation/                 # HTTP-роуты и UI оператора
    infrastructure/               # адаптеры хранилищ и интеграций API
    dependencies.py               # сборка контейнера зависимостей приложения
    run.py                        # bootstrap API + ожидание Postgres + миграции
  detection_service/
    domain/                       # модели и интерфейсы детекции
    application/                  # оркестрация обработки потока кадров
    infrastructure/               # интеграции с YAML, YOLO и HTTP
    presentation/                 # API для запуска и контроля стрима
  batch_runner/
    main.py                       # основной файл запуска батч-обработки

tests/
  architecture/                   # тесты границ слоев (import boundaries)
  test_*.py                       # unit / integration / e2e тесты

.github/workflows/                # сценарии автоматических проверок в GHA
docker-compose.yml                # основной online/pilot контур
Makefile
pyproject.toml
README.md
```

## Запуск через Docker

### Требования

- Docker Desktop / Docker Engine
- Свободный порт `8000`

### Подготовка данных миссии

Нужна локальная папка миссии со структурой:

```text
/
├── images/
│   ├── frame_0001.jpg
│   ├── frame_0002.jpg
│   └── ...
└── annotations/
    └── *.json   # COCO annotations
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

Для быстрого локального прогона можно использовать:

```env
APP_REPOSITORY_BACKEND=memory
ARTIFACTS_MODE=local
```

Как это работает:

- `APP_REPOSITORY_BACKEND=memory` — быстрый локальный режим без внешней БД.
- `APP_REPOSITORY_BACKEND=postgres` — режим с Postgres для operational state.
- `ARTIFACTS_MODE=local` — режим разработки, быстрых тестов и автономных демонстраций.
- `ARTIFACTS_MODE=s3` — целевой режим хранения артефактов для pilot/prod.

Что обязательно заполнить для полноценной записи артефактов в S3:

```env
ARTIFACTS_S3_ENDPOINT=...
ARTIFACTS_S3_REGION=...
ARTIFACTS_S3_ACCESS_KEY_ID=...
ARTIFACTS_S3_SECRET_ACCESS_KEY=...
ARTIFACTS_S3_BUCKET=...
ARTIFACTS_S3_STRICT=true
```

Если `ARTIFACTS_MODE=s3`, но включен `ARTIFACTS_S3_STRICT=false`, допускается controlled fallback на локальное хранилище только для dev/test сценариев. Локальная копия кадра также может временно использоваться для UI, пока артефакт отправляется в объектное хранилище.

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

## Postgres backend

Если нужен режим с Postgres, в основном `docker-compose.yml` используется опциональный профиль `postgres`.

В `.env` нужно явно задать параметры подключения:

```env
APP_REPOSITORY_BACKEND=postgres
APP_POSTGRES_HOST=<postgres-host>
APP_POSTGRES_PORT=<postgres-port>
APP_POSTGRES_DB=<postgres-db>
APP_POSTGRES_USER=<postgres-user>
APP_POSTGRES_PASSWORD=<postgres-password>
APP_POSTGRES_AUTO_MIGRATE=true
```

Либо можно задать единый DSN:

```env
APP_REPOSITORY_BACKEND=postgres
APP_POSTGRES_DSN=postgresql://<user>:<password>@<host>:<port>/<db>
APP_POSTGRES_AUTO_MIGRATE=true
```

Запуск:

```bash
docker compose --profile postgres up --build
```

Что важно:

- для режима `postgres` параметры подключения должны быть заданы явно;
- дефолтные `APP_POSTGRES_HOST/PORT/DB/USER` из шаблона убраны;
- пустой или неполный Postgres-конфиг считается ошибкой старта;
- при неверных credentials или несуществующей БД bootstrap падает сразу, а не продолжает запуск в некорректном состоянии;
- при старте API-контейнер дожидается доступности БД и применяет миграции из `db_migrations/`.

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
- [docs/architecture/batch_contour.md](docs/architecture/batch_contour.md) — архитектурная схема batch-контура.
- [docs/batch_evidence_pack.md](docs/batch_evidence_pack.md) — что собрать для защиты/сдачи.

Airflow оркестрирует запуск batch-runner контейнера через `DockerOperator`.

- Бизнес-логика находится в коде проекта (`libs/batch/application`), а DAG управляет расписанием и backfill.
- Идемпотентность обеспечивается `run_key` и статусами, чтобы повторный запуск на тот же ключ не создавал дублей без `--force`.
- В `local` окружении batch по умолчанию использует локальные storage-адаптеры.
- В `shared/stage/prod` окружениях batch по умолчанию использует `S3ArtifactStore` и `PostgresStatusStore`.

## CI/CD каркас

- [`.github/workflows/ci.yml`](.github/workflows/ci.yml) — основной CI: линтеры, тесты, архитектурные проверки, batch smoke и secret scanning.
- [`.github/workflows/infra-ci.yml`](.github/workflows/infra-ci.yml) — проверка инфраструктурного контура: валидность compose и DAG-артефактов.
- [`.github/workflows/batch-e2e.yml`](.github/workflows/batch-e2e.yml) — e2e backfill сценарий для batch-контура.
