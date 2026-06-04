# Drift setup — подготовка эталонной миссии и контроль PSI/CSI

## Зачем

Дипломка §2.7 описывает контроль дрейфа входных данных через индексы
PSI и CSI. Чтобы они были живыми (а не пустыми точками на дашборде),
нужны две вещи:

1. **Эталонная (reference) миссия** — конкретный mission_id из набора
   «Экстренный поиск», от которого считаем сдвиг.
2. **Активный daily DAG** — он на каждый ds считает PSI/CSI миссий
   этого ds относительно эталона и пишет в `drift_observations`.
   batch-exporter подтягивает в Prometheus gauges, дашборд оживает.

Эталон создаётся **один раз** (или при re-baseline) ручной командой;
дальше всё автоматически.

## Где это живёт в коде

| Слой | Файл | Что |
|---|---|---|
| Формулы PSI/CSI | `rescue_ai/domain/mission_metrics.py` | секция «Дрейф данных» в конце |
| Per-frame features | `rescue_ai/application/pipeline_stages.py` `_evaluate` | confidence_max, bbox_area_norm, bbox_ratio, brightness_mean |
| Запись в БД | `rescue_ai/infrastructure/batch_metrics_repository.py` | `save_drift_reference`, `save_drift_observation`, `load_current_drift_reference` |
| SQL миграция | `infra/postgres/init/040-drift.sql` | `drift_reference` + `drift_observations` |
| Стадия pipeline | `run_publish_metrics_stage` (тот же `publish_metrics`!) | дрейф встроен сюда, отдельного stage нет |
| Экспорт в Prometheus | `rescue_ai/interfaces/batch_exporter/run_service.py` | `_refresh_drift_gauges` |
| Метрики | `rescue_ai_drift_psi_score`, `rescue_ai_drift_csi{feature}` | таблица 3.8 пояснительной записки |
| Alertmanager rule | `infra/k8s/charts/rescue-ai-observability/files/prometheus/rules-central.yml` секция `rescue-ai-drift` | срабатывает при PSI > 0.2 или max(CSI) > 0.2 |

## Подготовка эталонной миссии

### Шаг 1. Выбрать миссию

Требования:
- из набора «Экстренный поиск» (по дипломке §2.2);
- кадры разнообразны по освещению / высоте / плотности;
- размечена (есть `labels.json` рядом с frames);
- **минимум 300–500 кадров**, оптимально ~1000 — иначе биннинг по
  10 интервалам страдает от sparse-buckets и PSI шумит.

Помести её в S3 по обычному батч-layout-у:
```
missions/ds=YYYY-MM-DD/<mission_id>/frames/*.jpg
missions/ds=YYYY-MM-DD/<mission_id>/labels.json
```

### Шаг 2. Прогнать через batch одной командой

С развёрнутым cloud-кластером и заполненным Vault:

```bash
# Шаг 2.1: prepare_dataset — собрать манифест из frames/labels.
python -m rescue_ai.interfaces.cli.batch \
    --stage prepare_dataset \
    --ds 2026-03-15 \
    --mission-ids-csv reference-mission-001

# Шаг 2.2: evaluate_model — прогнать YOLO, собрать TP/FN/FP/TN
# И автоматически сохранить per-frame features (confidence/bbox/brightness).
python -m rescue_ai.interfaces.cli.batch \
    --stage evaluate_model \
    --ds 2026-03-15 \
    --mission-ids-csv reference-mission-001

# Шаг 2.3: publish_metrics с флагом --as-reference — записать
# гистограммы в drift_reference (старый reference автоматически
# снимется с is_current=TRUE).
python -m rescue_ai.interfaces.cli.batch \
    --stage publish_metrics \
    --ds 2026-03-15 \
    --mission-ids-csv reference-mission-001 \
    --as-reference \
    --reference-id ref-2026-03-15-rescue-search
```

### Шаг 3. Проверить, что эталон сохранился

```bash
psql "$DB_DSN" -c "
  SELECT reference_id, mission_id, ds, n_samples, model_version, created_at
  FROM drift_reference
  WHERE is_current = TRUE;
"
```

Должна быть одна строка с твоим `reference_id`. Партичный UNIQUE индекс
`ux_drift_reference_current` гарантирует, что активным может быть
только один reference.

## Что происходит в daily DAG

После того как reference есть, обычный daily-прогон DAG-а делает на
стадии `publish_metrics`:

1. Для каждой миссии текущего ds читает `evaluation.json` (там лежат
   уже посчитанные `frame_features`).
2. Все features ds сливаются в совокупные гистограммы.
3. Загружается current `drift_reference`.
4. Считаются 1 × PSI (по confidence) + 3 × CSI (площадь bbox, его
   aspect ratio, brightness кадра).
5. Результат пишется в `drift_observations` (PK = ds, ON CONFLICT
   UPDATE — повторный прогон ds перезаписывает строку).
6. `batch-exporter` через 5 минут видит новую строку и обновляет
   gauges `rescue_ai_drift_psi_score`, `rescue_ai_drift_csi{feature}`.
7. Grafana «Data drift» оживает; Alertmanager rule срабатывает при
   PSI > 0.2 или max(CSI) > 0.2.

Никаких новых stage'ов в DAG нет — drift встроен внутрь существующей
третьей стадии.

## Когда менять reference (re-baseline)

- При смене модели (новые веса YOLO);
- При сезонном сдвиге (зима → лето), если PSI устойчиво стал > 0.2 при
  стабильном качестве модели;
- Раз в полгода-год как операционная гигиена.

Процедура та же: запустить `publish_metrics --as-reference
--reference-id ref-<новый-id>` для новой выбранной миссии. Старый
reference автоматически снимется с активного флага и останется в
таблице для истории.

## Проверка дашборда

После шага 2 (reference создан) и первого daily-прогона:

```bash
# Текущие drift-наблюдения
psql "$DB_DSN" -c "
  SELECT ds, psi_confidence, csi_bbox_area, csi_bbox_ratio,
         csi_brightness, drift_flag, n_samples, n_missions
  FROM drift_observations
  ORDER BY ds DESC LIMIT 5;
"

# Gauges в batch-exporter
kubectl -n rescue-batch port-forward svc/rescue-batch-rescue-ai-batch-exporter 8003:8003 &
curl http://localhost:8003/metrics | grep '^rescue_ai_drift_'
```

Должно быть видно 4 числа: `rescue_ai_drift_psi_score` и три точки
`rescue_ai_drift_csi{feature="bbox_area|bbox_ratio|brightness"}`.

## Связанные документы

- [Vault setup](vault_setup.md) — DB_DSN секрет
- [k3s field deploy](k3s_field_deploy.md) — offline (drift не нужен)
- [batch_namespace](batch_namespace.md) — где живёт DAG и exporter
- [batch_operations.md](batch_operations.md) — повседневная работа с DAG
