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
libs/
  core/
    domain/        # основные сущности миссии и алертов
    application/
      models.py          # модели входных данных и правила конфигурации
      contracts.py       # интерфейсы для работы с хранилищем
      alert_policy.py    # когда и как создавать алерты
      mission_metrics.py # расчет метрик по миссии
      pilot_service.py   # основной сценарий работы миссии
  infra/
    postgres/      # заготовка репозиториев для Postgres

services/
  api_gateway/
    presentation/   # HTTP-эндпоинты и страница оператора
    infrastructure/ # in-memory хранилище для локального запуска
    dependencies.py # сборка зависимостей приложения
  detection_service/
    domain/         # модели и интерфейсы детекции
    application/    # логика обработки потока кадров
    infrastructure/ # интеграции с YAML, YOLO и HTTP
    presentation/   # API для запуска и контроля стрима

configs/            # конфиги детекции и алертинга
artifacts/          # примеры данных миссий
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

## Инфраструктурный стенд (Airflow + Observability + S3 + Postgres)

В репозитории добавлен отдельный каркас платформы в папке `infra/`:

- `Airflow` (`webserver`, `scheduler`, `init`)
- `Postgres` + `postgres-exporter`
- `Prometheus`
- `Grafana` (provisioning datasource + dashboard)

Быстрый запуск:

```bash
cd infra
cp platform.env.example platform.env
docker compose -f docker-compose.platform.yml --env-file platform.env up -d
```

Ключевые UI:

- Airflow: `http://localhost:8080`
- Grafana: `http://localhost:3000`
- Prometheus: `http://localhost:9090`

Подробности: [infra/README.md](infra/README.md)

### Batch сервис (Airflow)

Подробный запуск и эксплуатация batch-контура вынесены в отдельные документы:

- `infra/README.md` — запуск платформы, DAG, backfill, idempotency.
- `docs/runbooks/batch_operations.md` — диагностика, safe rerun, runbook статусов/ошибок.
- `docs/runbooks/batch_demo_playbook.md` — сценарий real-data прогона и quality gates.
- `docs/batch_evidence_pack.md` — чеклист материалов для защиты.
- `docs/architecture/batch_contour.md` — архитектурная схема batch-контура.

## CI/CD каркас

- Базовый CI проекта: `.github/workflows/ci.yml`
- CI для инфраструктуры (`compose config`): `.github/workflows/infra-ci.yml`
- E2E для батча (nightly + manual): `.github/workflows/batch-e2e.yml`
