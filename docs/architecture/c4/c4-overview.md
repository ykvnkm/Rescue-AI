# C4: Архитектура Rescue-AI (обзор)

- Статус: Черновик
- Дата: 2026-03-08
- Автор: Максим Яковенко

## Назначение
Этот раздел описывает архитектуру системы Rescue-AI по C4 на уровнях L1–L3 (и опционально L4).
Цель: чтобы любой человек за 2–5 минут понял:
- кто пользуется системой и зачем,
- из каких крупных частей она состоит,
- как течёт данные “кадры → детекции → алерты → отчёт”,
- как это всё запускается сейчас (MVP) и что планируется для production.

## Источники правды (в репозитории)
Схема строится по реальным файлам/точкам входа:

- Запуск приложения (FastAPI):  
  `services/api_gateway/app.py`
- HTTP-маршруты (UI + API):  
  `services/api_gateway/presentation/http/routes.py`  
  `services/api_gateway/presentation/http/ui_page.py`  
  `services/api_gateway/presentation/http/templates/`
- Сборка зависимостей (in-memory репозитории + PilotService + YAML-контракт):  
  `services/api_gateway/dependencies.py`
- Запуск/управление стримом (оркестратор, конфиг стрима, статусы):  
  `services/detection_service/presentation/stream_api.py`  
  `services/detection_service/application/stream_orchestrator.py`  
  `services/detection_service/application/stream_config.py`  
  `services/detection_service/application/frame_source.py`
- Детектор и рантайм-контракт (YAML):  
  `services/detection_service/infrastructure/runtime_contract.py`  
  `services/detection_service/infrastructure/yolo_detector.py`  
  `configs/nsu_frames_yolov8n_alert_contract.yaml`
- Бизнес-логика пилота (алерты, правила, метрики, отчёт):  
  `libs/core/application/pilot_service.py`  
  `libs/core/application/alert_policy.py`  
  `libs/core/application/mission_metrics.py`

## Как запускается сейчас (MVP)
В MVP система поднимается одним Docker-контейнером `api` (FastAPI) и использует тома:
- `MISSION_DIR -> /data/mission:ro` (кадры и, при наличии, аннотации миссии)
- `./runtime -> /app/runtime` (локальные runtime-артефакты/кэш)

См. `docker-compose.yml` и `.env.example`.

## Диаграммы C4
- L1 — Контекст системы: [c4-L1-system-context.md](./c4-L1-system-context.md)
- L2 — Контейнеры и внешние зависимости: [c4-L2-container-view.md](./c4-L2-container-view.md)
- L3 — Компоненты контейнера `api` (FastAPI + streaming loop): [c4-L3-api-components.md](./c4-L3-api-components.md)
- (опционально) L4 — Развёртывание (MVP vs production): [c4-L4-deployment-view.md](./c4-L4-deployment-view.md)

## Связь с ADR
- ADR-0001: размещение инференса (ground vs edge) — влияет на L4 и внешний видеоканал
- ADR-0002: приём видеопотока (RTSP/UDP + офлайн) — влияет на источник кадров в L2/L3
- ADR-0003: контракт алертинга и метрики пилота — влияет на логику core в L3
- ADR-0004: выбор базового детектора (YOLOv8n) — влияет на runtime в L3

## Ссылки
- [ML System Design Doc](../../ml_system_design_doc.md)
- [Docker Compose](../../../docker-compose.yml)
- [YAML-контракт (модель + алертинг)](../../../configs/nsu_frames_yolov8n_alert_contract.yaml)
- [ADR](../../adr/)