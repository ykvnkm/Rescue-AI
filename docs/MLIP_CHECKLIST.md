# ML in Prod Checklist (Local)

Локальный чеклист прогресса по `Rescue-AI` (ориентир: `docs/ml_system_design_doc.md` + текущее состояние репозитория).

## Что уже сделано

- [x] GitHub-процесс: ветки, PR, история коммитов.
- [x] Базовая структура под Clean Architecture: `libs/core`, `libs/infra`, `services/api_gateway`.
- [x] REST API и UI для пилота:
  - [x] `POST /v1/missions/start-flow` (создание миссии + старт потока),
  - [x] `GET /v1/missions/{mission_id}/stream/status`,
  - [x] `POST /v1/missions/{mission_id}/complete`,
  - [x] `POST /v1/missions/{mission_id}/frames`,
  - [x] `GET /v1/alerts`, `GET /v1/alerts/{alert_id}`, `GET /v1/alerts/{alert_id}/frame`,
  - [x] `POST /v1/alerts/{alert_id}/confirm|reject`,
  - [x] `GET /v1/missions/{mission_id}/report`,
  - [x] сервисные `GET /health`, `GET /ready`, `GET /version`, Swagger `/docs`.
- [x] Бизнес-правила алертинга реализованы в коде: `conf_thr=0.2`, `W=1s`, `k=2`, `cooldown=2s`, `τ_gap_end=1s`.
- [x] TtFC в сервисе приведен к логике из SDD: первый GT-эпизод + первый подтверждённый алерт в допуске `±τ`.
- [x] `FP/min` (в минуту, не в час) реализован в отчете.
- [x] `people_detected` = количеству bbox/детекций в кадре алерта.
- [x] Временный операторский UI приведен к “миссионному” виду (крупный алерт-кадр + confirm/reject + таблица отчета на русском).
- [x] Unit-тесты: стабильный проход, coverage > 70%.
- [x] CI: `black`, `isort`, `flake8`, `mypy`, `pylint`, `pytest`.
- [x] `uv`-пин зависимостей: `pyproject.toml` + `uv.lock`.
- [x] Docker-артефакты: `Dockerfile`, `docker-compose.yml`.
- [x] ML SDD существенно обновлен (разделы 1–3 + ссылки/форматирование).

## Что не сделано / частично сделано

### По продукту (SDD)
- [ ] Реальная модель детекции не подключена в онлайн-контур (сейчас stream имитируется GT/label-based runner).
- [ ] Поток с Raspberry Pi не подключен (пока офлайн-папка кадров).
- [ ] Полноценная проверка пилота на 12 миссиях с финальными артефактами отчёта ещё не зафиксирована как отдельный эксперимент-пакет.

### По требованиям курса
- [ ] Полное завершение Clean Architecture для `detection_service` и `navigation_service` (сейчас фактически работает только `api_gateway`).
- [ ] README требует финального прохода под текущее состояние API/UI/запуска (синхронизировать с фактическими endpoint и сценариями).
- [ ] YAML-описание каждого endpoint (отдельный OpenAPI YAML в `docs/api/`) отсутствует.
- [ ] CI/CD deploy на удалённый сервер (push-модель) отсутствует.
- [ ] Публикация docker image в registry отсутствует.
- [ ] Airflow batch-контур (`DAG`, `DockerOperator`, idempotency, backfill) отсутствует.
- [ ] Мониторинг/алертинг (`Prometheus`, `Grafana`, Telegram/email alerts) отсутствует.
- [ ] ML-мониторинг (качество, PSI/CSI) отсутствует.
- [ ] External Postgres пока не подключен.
- [ ] S3-only хранение артефактов пока не реализовано end-to-end.
- [ ] ADR/C4 созданы как заготовки, но не финализированы по фактической архитектуре.

## Что делать дальше (приоритет)

1) **Архитектурная фиксация**  
- [ ] Закрепить границы сервисов: что живет в `api_gateway`, что уходит в отдельные `detection_service` / `navigation_service`.
- [ ] Финализировать ADR + C4 под реальное текущее решение.

2) **Контракты и документация API**
- [ ] Собрать единый `docs/api/openapi.yaml`.
- [ ] Обновить README под текущий one-click flow (`start-flow`) и операторский UI.

3) **Данные/хранилища**
- [ ] Подключить external Postgres.
- [ ] Перевести артефакты/кадры/отчеты на S3-only.

4) **Эксплуатационный контур**
- [ ] Добавить Prometheus + Grafana + alerting.
- [ ] Добавить метрики качества/дрейфа (PSI/CSI, где применимо).

5) **Batch и MLOps**
- [ ] Airflow DAG + DockerOperator + idempotency + backfill-demo.
- [ ] CI/CD deploy + image publish.
