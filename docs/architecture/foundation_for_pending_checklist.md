# Foundation for Pending Checklist Items

Этот документ фиксирует базовую архитектуру для задач, которые еще не закрыты в чеклисте.

## 1. Batch + Airflow

- `services/batch_service/domain`: бизнес-сущности батч-джоба (`BatchJob`, `BatchResult`).
- `services/batch_service/application`: use-case `RunBatchJob` и порты (`BatchExecutor`, `ArtifactStore`, `JobRepository`, `MetricsPublisher`).
- `services/batch_service/infrastructure`: адаптеры (локальный раннер, S3 store, репозиторий, publisher).
- `orchestration/airflow/dags`: DAG оркестрирует шаги и не содержит бизнес-правил.

## 2. Remote Artifact Storage

- Контракт хранения задан в `ArtifactStore` (application слой).
- Реализация S3 вынесена в инфраструктуру: `s3_artifact_store.py`.
- Конфиг-шаблон вынесен в `configs/artifact_store.example.yaml`.

## 3. Monitoring and Alerting

- `deploy/monitoring/prometheus`: scrape-конфиг и алерт-правила.
- `deploy/monitoring/grafana`: provisioning + стартовый dashboard.
- Используется принцип: наблюдаемость подключается как внешний слой без зависимости домена.

## 4. CI/CD

- `.github/workflows/cd.yml` выделен отдельно от `ci.yml`.
- Фазы: publish image -> deploy.
- Конкретный remote deploy остается адаптером окружения (через secrets и отдельный deploy-командный блок).

