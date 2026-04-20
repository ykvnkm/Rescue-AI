# ADR-0006: Разделение режимов миссии — operator vs automatic

- Статус: Принято
- Дата: 2026-04-20
- Авторы: Максим Яковенко

## Контекст

Система Rescue-AI изначально проектировалась под **операторский** сценарий: видео/кадры с БПЛА → детекция людей → алерты → ручное подтверждение/отклонение оператором. Вся доменная модель (`Mission`, `FrameEvent`, `Alert`, `Episode`) построена вокруг этого потока.

Параллельно существует репозиторий [diplom-prod](https://github.com/ykvnkm/diplom-prod), в котором реализована **автоматическая** ветка: БПЛА передаёт видеопоток/RTSP, система вычисляет траекторию (через маркер или optical flow), выдаёт детекции и решения **без участия оператора**. Логика разнесена по трём мегафайлам (`unified_navigation_service.py` ~3800 LoC, `rpi_source_service.py`, `detection_service.py`), дополнительно используется nanodet-детектор.

Задача — влить эту автоматическую ветку в Rescue-AI **без потери чистой архитектуры** и **без дублирования** существующей бизнес-логики (алерт-политика, миссии, отчётность).

### Ключевые различия режимов

| Аспект | Operator | Automatic |
|---|---|---|
| Источник кадров | HTTP ingest / RTSP от RPi | RTSP / видео / папка кадров |
| Детектор | YOLOv8n | YOLOv8n **или** NanoDet |
| Навигация | нет | marker PnP / optical-flow / гибрид с auto-probe |
| Проверка оператором | обязательна (`reviewed_by`, `decision_reason`) | отсутствует |
| Принятие решения | QUEUED → CONFIRMED/REJECTED оператором | автоматический лог `AutoDecision` |
| Отчёт | frames + alerts + episodes | + trajectory CSV + 3D/top-down плоты |
| Выходные артефакты в S3 | `frames/*.jpg`, `report.json` | + `trajectory.csv`, `plots/*.png` |

## Решение

Используем **гибридную модель** (вариант C из обсуждения):

1. **Единый агрегат `Mission`** с дискриминатором `mode: MissionMode = OPERATOR | AUTOMATIC`.
2. **Общие таблицы** (`missions`, `frame_events`, `alerts`) — остаются, получают колонку `missions.mode`.
3. **Таблицы-спутники для automatic-специфики** — не пересекаются с operator-полями:
   - `auto_trajectory_points(mission_id, ts_sec, x, y, z, source)` — 3D-точки траектории;
   - `auto_decisions(mission_id, frame_id, decision, reason, created_at)` — лог автоматических решений (аналог operator review, но без человека);
   - `auto_mission_config(mission_id, nav_mode, detector, nav_config_json)` — снимок конфигурации запуска.
4. **Operator-специфичные поля** (`reviewed_by`, `reviewed_at_sec`, `decision_reason` в `alerts`) остаются в основной таблице, но для automatic-миссий **не заполняются** (остаются NULL).
5. **Два application-сервиса**: существующий `PilotService` (operator) и новый `AutoMissionService` (automatic). Общий алерт-policy (`domain/alert_policy.py`) переиспользуется для обоих — sliding-window + quorum + cooldown применимы в обоих режимах.
6. **Два пакета интерфейса**: существующие `/missions/*` и новые `/auto-missions/*`. CLI-команды `rescue-ai online` и `rescue-ai auto-run` отдельные.
7. **Порт `NavigationEnginePort`** добавляется в домен и реализуется в `rescue_ai/navigation/` (pure Python, без FastAPI/OpenCV в сигнатурах — OpenCV только в реализации).

### Схема доменной иерархии

```
domain/
  entities.py
    Mission (+ mode: MissionMode)
    FrameEvent
    Detection
    Alert
    TrajectoryPoint    # new
    AutoDecision       # new
  value_objects.py
    MissionMode (OPERATOR | AUTOMATIC)
    AlertStatus, AlertRuleConfig, ArtifactBlob
    NavMode (MARKER | NO_MARKER | AUTO)   # new
  ports.py
    MissionRepository          (extended: filter by mode)
    AlertRepository
    FrameEventRepository
    TrajectoryRepository       # new
    AutoDecisionRepository     # new
    ArtifactStorage            (extended: save_trajectory, save_plots)
    DetectorPort               (used by both modes)
    NavigationEnginePort       # new — pure function frame→pose
    VideoFramePort             # new — stream abstraction
```

## Рассмотренные альтернативы

1. **(A) Полиморфизм через nullable поля** — общие таблицы со всеми полями, для automatic-миссий operator-поля NULL. Минус: размазывается смысл NOT NULL, запросы вида "миссии, не подтверждённые оператором" становятся неоднозначными (automatic ≠ непроверенная operator).
2. **(B) Полное разделение агрегатов** — отдельные `OperatorMission`/`AutoMission`, раздельные таблицы, дублирующиеся репозитории/API. Минус: дублирование логики алертов, два source-of-truth для статистики.
3. **(C) Гибрид (выбранный)** — общий агрегат + дискриминатор + таблицы-спутники для несовпадающих частей. Лучший компромисс между чистотой DDL и минимумом дублирования.

## Последствия

### Плюсы

- Алерт-policy и метрики миссии переиспользуются без изменений — уже покрыты тестами, proven в проде.
- Миграция БД аддитивна: только `ADD COLUMN` к `missions` + три новые таблицы. Существующие данные не трогаются.
- API существующих маршрутов `/missions/*` не ломается — новые маршруты живут под `/auto-missions/*`.
- Batch-pipeline (`pipeline_stages.py`) остаётся без изменений в scope этого ADR (operator-only). Расширение на automatic — отдельный вопрос для следующего ADR после P1.

### Минусы

- Колонка `missions.mode` требует backfill'а (все существующие миссии = OPERATOR). Делается одним UPDATE.
- Два application-сервиса увеличивают cognitive load при чтении кода — смягчается разделением по папкам `application/operator/` vs `application/automatic/`.
- Operator-поля в `alerts` для automatic-миссий NULL — нужно явное соглашение в коде/документации, что это валидное состояние.

## Дальнейшие шаги

1. Миграция `infra/postgres/init/011-auto-mode-schema.sql` с ADD COLUMN + 3 новые таблицы (реализуется в P1.1).
2. Backfill существующих миссий: `UPDATE missions SET mode='operator' WHERE mode IS NULL` (в той же миграции).
3. Обновить architecture-тесты (`tests/architecture/`) — убедиться, что `navigation/` не импортирует инфраструктуру, а automatic-сервис не тянет operator-поля.
4. После P1.5 — решить, распространять ли batch-pipeline на automatic-миссии (возможно, отдельный DAG).

## Ссылки

- [Текущая доменная модель](../../rescue_ai/domain/entities.py)
- [Алерт-policy — общая для обоих режимов](../../rescue_ai/domain/alert_policy.py)
- [Текущий PilotService](../../rescue_ai/application/pilot_service.py)
- [diplom-prod: unified_navigation_service.py](https://github.com/ykvnkm/diplom-prod/blob/main/services/unified_runtime/unified_navigation_service.py)
- ADR-0007 — Автономный деплой и offline-синхронизация
- ADR-0008 — Kubernetes и управление секретами
