# Alert Lifecycle Contract

## Цель
Зафиксировать жизненный цикл алерта и действия оператора.

## Entity: Alert
- `alert_id` (string, UUID)
- `mission_id` (string)
- `frame_id` (int)
- `ts_sec` (float)
- `score` (float, 0..1)
- `bbox` (array[4]) — `[x1, y1, x2, y2]`
- `status` (enum, см. ниже)
- `created_at` (ISO8601)
- `reviewed_at` (ISO8601 | null)
- `reviewed_by` (string | null)
- `decision_reason` (string | null)

## Status Model
- `new` — создан моделью
- `queued` — показан в очереди UI
- `reviewed_confirmed` — оператор подтвердил
- `reviewed_rejected` — оператор отклонил

Переходы:
- `new -> queued`
- `queued -> reviewed_confirmed`
- `queued -> reviewed_rejected`

Обратные переходы запрещены.

## API Actions (логический контракт)
- `POST /v1/alerts/{alert_id}/confirm`
- `POST /v1/alerts/{alert_id}/reject`
- `GET /v1/alerts?mission_id=...&status=...`

## PostgreSQL
- `alerts` — карточка алерта + текущий статус
- `operator_decisions` — журнал решений оператора (append-only)
