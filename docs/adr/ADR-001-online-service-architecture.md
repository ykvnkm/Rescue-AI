# ADR-001: Online Service Architecture

- Status: Proposed
- Date: 2026-02-18
- Owners: TBD

## Context

Нужна online-часть сервиса с REST API, health/readiness checks, CI quality gates и возможностью безопасного расширения до production-сценариев (S3, Postgres, monitoring, alerting).

## Decision

Принять модульную архитектуру online-сервиса с разделением по слоям:

- `presentation` (HTTP handlers, API schemas)
- `application` (use cases, orchestration)
- `domain` (entities, business rules, interfaces)
- `infrastructure` (S3/Postgres/queue/clients adapters)

Публичный вход в систему:

- API gateway (FastAPI)
- Контракты между модулями через явно типизированные модели

## Alternatives Considered

1. Монолит без слоёв
- Pros: быстрее старт.
- Cons: высокий технический долг, сложно тестировать и масштабировать.

2. Микросервисы с первого дня
- Pros: максимальная изоляция.
- Cons: лишняя операционная сложность на раннем этапе.

## Consequences

### Positive

- Проще наращивать функционал predict/monitoring без слома API.
- Упрощается unit/integration testing.
- Подходит под требования курса по Clean Architecture.

### Negative

- Больше шаблонного кода на старте.
- Требует дисциплины в границах слоев.

## Implementation Notes

- Вести все бизнес-правила в `domain/application`, не в роутерах.
- Не смешивать инфраструктурный код с use-cases.
- Обязательные quality gates: tests + linters + typing в CI.

## Validation

Критерии завершения ADR:

- Архитектура отражена в структуре каталогов.
- Для ключевых сценариев есть unit tests.
- API слой не содержит бизнес-логики.

## Follow-up

- Уточнить transport между online и batch компонентами.
- Уточнить формат хранения артефактов в S3.
