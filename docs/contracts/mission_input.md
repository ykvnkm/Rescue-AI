# Mission Input Contract

## Цель
Зафиксировать единый формат входа для пилотной миссии (кадры + GT), чтобы одинаково работали API, пайплайн и отчёты.

## Entity: Mission
- `mission_id` (string, UUID)
- `source_name` (string) — имя набора/сценария
- `created_at` (ISO8601)
- `status` (enum): `created | running | completed | failed`
- `total_frames` (int)
- `fps` (float)

## Entity: Frame Event
- `mission_id` (string)
- `frame_id` (int, >= 0)
- `ts_sec` (float) — время от начала миссии в секундах
- `image_uri` (string) — путь/ссылка на кадр
- `gt_person_present` (bool) — есть ли человек в кадре по GT
- `gt_episode_id` (string | null) — id эпизода присутствия человека

## Ingestion Rules
1. `frame_id` строго возрастает внутри миссии.
2. `ts_sec` не убывает.
3. Один кадр = одно событие Frame Event.
4. Если `gt_person_present=false`, то `gt_episode_id=null`.

## API Start Contract
- `POST /v1/missions/start-flow` запускает миссию одним шагом.
- Обязательные поля:
  - `frames_dir` (string, абсолютный путь до кадров),
  - `fps` (float, > 0),
  - `source_name` (string).
- Опциональные поля:
  - `labels_dir` (string | null),
  - `high_score` (float),
  - `low_score` (float),
  - `api_base` (string).

## PostgreSQL (source of truth)
- `missions` — метаданные миссии
- `mission_frames` — реестр кадров/GT (минимально: frame_id, ts_sec, gt flags)
