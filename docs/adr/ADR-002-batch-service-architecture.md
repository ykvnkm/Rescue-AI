# ADR-002: Batch Service Architecture

- Status: Proposed
- Date: 2026-02-18
- Owners: TBD

## Context

Нужен batch-контур на Airflow, который выполняет периодические задачи на базе кода проекта, поддерживает идемпотентность и backfill.

## Decision

Использовать Airflow DAG с `DockerOperator` для выполнения batch-задач в контейнерах.

Базовый конвейер:

1. Extract (получение входных данных / событий)
2. Transform (обработка и подготовка фич/агрегатов)
3. Validate (контроли качества данных)
4. Load (запись результатов в Postgres/S3)

## Idempotency Strategy

- Вводить `run_key`/`window_key` для каждого окна обработки.
- Повторный запуск должен обновлять или переиспользовать существующий результат без дублей.
- Запись результатов делать через upsert-паттерн.

## Backfill Strategy

- Backfill задается диапазоном дат (start/end).
- Каждый интервал обрабатывается независимо.
- Повторный backfill не должен ломать существующие результаты.

## Alternatives Considered

1. Cron + scripts
- Pros: простота.
- Cons: слабая наблюдаемость, сложнее управлять retries/backfill.

2. Внешний managed orchestrator
- Pros: меньше ops.
- Cons: лишняя сложность и vendor lock для учебного этапа.

## Consequences

### Positive

- Явный контроль расписания, retries и зависимости задач.
- Хорошая база для демонстрации backfill.

### Negative

- Дополнительный инфраструктурный слой.
- Нужно следить за версионированием docker-образов batch-задач.

## Validation

Критерии завершения ADR:

- Есть рабочий DAG с `DockerOperator`.
- Продемонстрированы идемпотентность и backfill.
- Результаты пишутся в целевые хранилища без дублей.

## Follow-up

- Зафиксировать схему данных для batch outputs.
- Добавить runbook по эксплуатации DAG и восстановлению после ошибок.
