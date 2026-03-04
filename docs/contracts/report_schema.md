# Pilot Report Contract

## Цель
Зафиксировать структуру итогового отчёта по миссиям и агрегата пилота.

## Report Level 1: Per Mission
- `mission_id`
- `ttfc_sec` — Time to first confirmation
- `recall_event` — полнота по эпизодам
- `fp_per_minute` — ложные тревоги в минуту
- `alerts_total`
- `alerts_confirmed`
- `alerts_rejected`
- `episodes_total`
- `episodes_found`
- `generated_at` (ISO8601)
- `config_name` — имя зафиксированного runtime-конфига
- `config_hash` — SHA-256 текущего config-файла
- `config_path` — путь до используемого config-файла
- `model_url` — ссылка на используемую модель
- `service_version` — версия сервиса (например, commit SHA)

Формулы:
- `TtFC = t_confirm_first - t_start`
- `Recall_event = episodes_found / episodes_total`
- `FP/min = false_positive_count / mission_duration_minutes`

## Report Level 2: Pilot Aggregate
- `missions_total`
- `ttfc_median_sec`
- `recall_event_mean`
- `fp_per_minute_mean`
- `created_at` (ISO8601)

## Output Format (MVP)
JSON + опционально CSV-экспорт.

## PostgreSQL
- `mission_reports` — per-mission метрики
- `pilot_reports` — агрегат по пилоту
