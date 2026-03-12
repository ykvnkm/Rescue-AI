# Batch Demo Playbook (Real Data, YOLO)

## Цель
Показать end-to-end batch-контур на реальных данных: backfill, idempotency, артефакты и метрики качества.

## Предусловия
- `BATCH_DETECTOR_BACKEND=yolo`
- В `BATCH_MISSION_ROOT/<mission_id>/<ds>/images` и `annotations/` лежат реальные данные.
- Платформа поднята (`infra/docker-compose.platform.yml`).

## Рекомендуемый сценарий
- `mission_id`: `pilot_eval_mission`
- `date range`: `2026-03-01..2026-03-03`
- Запуск через Airflow backfill.

## Команды

```bash
cd infra
cp platform.env.example platform.env
# Важно: yolo для демо/оценки
sed -i 's/^BATCH_DETECTOR_BACKEND=.*/BATCH_DETECTOR_BACKEND=yolo/' platform.env

docker compose -f docker-compose.platform.yml --env-file platform.env up -d

docker compose -f docker-compose.platform.yml --env-file platform.env exec airflow-webserver \
  airflow dags backfill rescue_batch_daily -s 2026-03-01 -e 2026-03-03
```

## Проверка артефактов и статусов

```bash
docker compose -f docker-compose.platform.yml --env-file platform.env exec airflow-webserver \
  ls -la /opt/airflow/data/artifacts /opt/airflow/data/status
```

## Quality gates
- `recall_event >= 0.7` (или проектный порог 0.9 для строгой оценки)
- `fp_per_minute <= 5`
- `ttfc_sec <= 6.5`

Проверка report:

```bash
uv run python scripts/batch/check_report_quality.py \
  --report /path/to/report.json \
  --min-recall 0.7 \
  --max-fp-per-minute 5 \
  --max-ttfc-sec 6.5
```

## Проверка idempotency

```bash
docker compose -f docker-compose.platform.yml --env-file platform.env exec airflow-webserver \
  uv run python -m services.batch_runner.main --mission-id pilot_eval_mission --ds 2026-03-01

docker compose -f docker-compose.platform.yml --env-file platform.env exec airflow-webserver \
  uv run python -m services.batch_runner.main --mission-id pilot_eval_mission --ds 2026-03-01
```

Ожидание: второй запуск без `--force` возвращает `idempotent_skip`.
