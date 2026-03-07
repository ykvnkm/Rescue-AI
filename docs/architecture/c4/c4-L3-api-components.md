# C4 L3: Компоненты контейнера `api` (FastAPI + streaming loop)

- Статус: Черновик
- Дата: 2026-03-08
- Автор: Максим Яковенко, Провков Иван, Скрыпник Михаил

## Описание
На уровне L3 раскрываем компоненты внутри одного развёртываемого контейнера `api`:
- HTTP-контур (UI + REST)
- Streaming-контур (перебор кадров, инференс)
- Core-контур (алертинг, метрики, отчёт)
- Хранилища (пока in-memory, далее — Postgres/S3)

Ключевая особенность MVP: streaming-контур публикует события кадров через HTTP обратно в тот же сервис
(“loopback”: `HttpFramePublisher -> /v1/missions/{id}/frames`).

## Диаграмма (L3)

```mermaid
flowchart TB
  %% ========== HTTP слой ==========
  subgraph HTTP["HTTP слой (API + UI)"]
    ROUTES["routes.py: /health, /, /v1/missions/start-flow, /v1/missions/:id/frames, /v1/alerts/*, /report"]
    UI["ui_page.py + templates: HTML UI оператора"]
    DEPS["dependencies.py: DI PilotService + in-memory repos + загрузка YAML-контракта"]
  end

  %% ========== Streaming ==========
  subgraph STREAM["Streaming / Detection контур"]
    SAPI["stream_api.py: build_stream_config, start, stop, status"]
    ORCH["stream_orchestrator.py: потоковый прогон кадров, thread, stop flag, state"]
    FS["frame_source.py: list frames, вычисление ts_sec"]
    AIDX["annotation_index.py: индексация GT, опционально"]
    DET["yolo_detector.py: DetectorPort.predict(frame_path)"]
    RCON["runtime_contract.py: load_stream_contract, YAML -> inference + alert rules"]
    PUB["http_publisher.py: POST frame-event в API"]
  end

  %% ========== Core ==========
  subgraph CORE["Core (бизнес-логика пилота)"]
    PS["pilot_service.py: создание миссии, ingest_frame_event, review_alert, report"]
    APOL["alert_policy.py: правила detections->alerts, conf_thr, window, k, cooldown, gap_end, tau"]
    MM["mission_metrics.py: Recall_event, FP/min, TtFC, агрегаты"]
    MODELS["models.py: контракты входов и выходов"]
  end

  %% ========== Хранилища ==========
  subgraph STORE["Хранилища / репозитории"]
    MEM["InMemory Repository (memory_store.py), MVP"]
    PG["Postgres, планируется"]
    S3["S3, планируется"]
  end

  %% -------- Связи HTTP --------
  UI --> ROUTES
  ROUTES --> DEPS
  ROUTES -->|start-flow| SAPI
  ROUTES -->|ingest frame-event| PS
  ROUTES -->|confirm/reject| PS
  ROUTES -->|report| PS

  %% -------- DI / Core --------
  DEPS -->|создаёт| PS
  PS --> APOL
  PS --> MM
  PS --> MEM

  %% -------- Streaming --------
  SAPI -->|build config| RCON
  SAPI -->|start| ORCH
  ORCH --> FS
  ORCH --> AIDX
  ORCH --> DET
  ORCH --> PUB
  PUB -->|POST /v1/missions/:id/frames| ROUTES

  RCON -->|model_url + alert rules| DET
  RCON -->|alert rules| DEPS

  %% -------- Планы --------
  MEM -.-> PG
  MEM -.-> S3
```
