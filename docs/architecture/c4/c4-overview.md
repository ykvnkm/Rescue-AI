# C4 Overview (Draft)

- Status: Draft
- Date: 2026-02-18

## Scope

Этот документ фиксирует C4-представление системы на уровнях L1-L3 и будет уточняться по мере реализации сервисов.

## C4 Level 1: System Context

Система взаимодействует с:

- Оператором (через REST/Swagger UI)
- Источниками видео/событий
- Postgres (external)
- S3-хранилищем артефактов
- Monitoring stack (Prometheus/Grafana/Alerting)

## C4 Level 2: Container View

Планируемые контейнеры:

- API Gateway (REST, OpenAPI)
- Online processing service
- Batch processing service (Airflow + task images)
- Postgres
- S3-compatible storage
- Monitoring/alerting stack

## C4 Level 3: Component View (initial)

### API Gateway

- Routing layer
- Request/response schemas
- Health/readiness/version endpoints

### Online Service

- Use-case orchestrator
- Domain logic
- Infrastructure adapters (S3, DB, external model runtime)

### Batch Service

- DAG definitions
- Batch task runners
- Data quality checks

## Open Items

- Уточнить финальные границы между online и batch processing.
- Зафиксировать контракт данных между сервисами.
- Добавить диаграммы (PlantUML/Mermaid) после стабилизации сервисов.
