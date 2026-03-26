# ML in Prod Checklist (Local)

Локальный чеклист прогресса по `Rescue-AI` (ориентир: `docs/ml_system_design_doc.md` + текущее состояние репозитория).

## Что уже сделано

- [x] GitHub-процесс: ветки, PR, история коммитов.
- [x] Базовая структура под Clean Architecture: `rescue_ai/domain`, `rescue_ai/application`, `rescue_ai/infrastructure`, `rescue_ai/interfaces`.
- [x] REST API и UI для пилота:
  - [x] `POST /v1/missions/start-flow` (создание миссии + старт потока),
  - [x] `GET /v1/missions/{mission_id}/stream/status`,
  - [x] `POST /v1/missions/{mission_id}/complete`,
  - [x] `POST /v1/missions/{mission_id}/frames`,
  - [x] `GET /v1/alerts`, `GET /v1/alerts/{alert_id}`, `GET /v1/alerts/{alert_id}/frame`,
  - [x] `POST /v1/alerts/{alert_id}/confirm|reject`,
  - [x] `GET /v1/missions/{mission_id}/report`,
  - [x] сервисные `GET /health`, `GET /ready`, `GET /version`, Swagger `/docs`.
- [x] Бизнес-правила алертинга реализованы в коде и читаются из `configs/nsu_frames_yolov8n_alert_contract.yaml`.
- [x] TtFC в сервисе приведен к логике из SDD: первый GT-эпизод + первый подтверждённый алерт в допуске `±τ`.
- [x] `FP/min` (в минуту, не в час) реализован в отчете.
- [x] `people_detected` = количеству bbox/детекций в кадре алерта.
- [x] Временный операторский UI приведен к “миссионному” виду (крупный алерт-кадр + confirm/reject + таблица отчета на русском).
- [x] Unit-тесты: стабильный проход, coverage > 70%.
- [x] CI: `black`, `isort`, `flake8`, `mypy`, `pylint`, `pytest`.
- [x] CI architecture guards: запрет прямых импортов `batch application -> batch infrastructure`, `api_gateway routes/dependencies -> detection_service`.
- [x] `uv`-пин зависимостей: `pyproject.toml` + `uv.lock`.
- [x] Docker-артефакты: `Dockerfile`, `docker-compose.yml`.
- [x] README обновлен: краткая логика, структура проекта, запуск и deploy через Docker, шаги воспроизведения результата.
- [x] ML SDD существенно обновлен (разделы 1–3 + ссылки/форматирование).

## Что не сделано / частично сделано

### По продукту (SDD)
- [x] Реальная модель детекции подключена в online-контур (YOLO в `detection_service`).
- [ ] Поток с Raspberry Pi не подключен (пока офлайн-папка кадров).
- [ ] Полноценная проверка пилота на 12 миссиях с финальными артефактами отчёта ещё не зафиксирована как отдельный эксперимент-пакет.

### По требованиям курса
- [ ] Полное завершение Clean Architecture для всех запланированных сервисов (в том числе будущего `navigation_service`).
- [x] Убраны прямые зависимости `api_gateway` на `detection_service` из `routes.py` и `dependencies.py` через infra-adapter.
- [x] README синхронизирован с текущим API/UI и Docker-сценарием запуска.
- [ ] YAML-описание каждого endpoint (отдельный OpenAPI YAML в `docs/api/`) отсутствует.
- [ ] CI/CD deploy на удалённый сервер (push-модель) отсутствует.
- [ ] Публикация docker image в registry отсутствует.
- [x] Airflow batch-контур (`DAG`, `DockerOperator`, idempotency, backfill) добавлен.
- [ ] Мониторинг/алертинг (`Prometheus`, `Grafana`, Telegram/email alerts) отсутствует.
- [ ] ML-мониторинг (качество, PSI/CSI) отсутствует.
- [ ] External Postgres пока не подключен.
- [ ] S3-only хранение артефактов пока не реализовано end-to-end.
- [ ] ADR/C4 созданы как заготовки, но не финализированы по фактической архитектуре.

## Что делать дальше (приоритет)

1) **Архитектурная фиксация**  
- [ ] Закрепить границы сервисов: что живет в `api_gateway`, что уходит в отдельный `detection_service`.
- [ ] Финализировать ADR + C4 под реальное текущее решение.

2) **Контракты и документация API**
- [ ] Собрать единый `docs/api/openapi.yaml`.
- [x] README под текущий one-click flow (`start-flow`) и операторский UI.

3) **Данные/хранилища**
- [ ] Подключить external Postgres.
- [ ] Перевести артефакты/кадры/отчеты на S3-only.

4) **Эксплуатационный контур**
- [ ] Добавить Prometheus + Grafana + alerting.
- [ ] Добавить метрики качества/дрейфа (PSI/CSI, где применимо).

5) **Batch и MLOps**
- [x] Airflow DAG + DockerOperator + idempotency + backfill-demo.
- [x] Batch-слой собран внутри `rescue_ai/application` и `rescue_ai/infrastructure`, composition root в `rescue_ai/interfaces/cli/batch.py`.
- [x] Добавлены batch unit/smoke/architecture тесты и CI-gates.
- [ ] CI/CD deploy + image publish.
