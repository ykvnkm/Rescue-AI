# C4 L2: Контейнеры и внешние зависимости (Container View)

- Статус: Черновик
- Дата: 2026-03-08
- Автор: Максим Яковенко, Провков Иван, Скрыпник Михаил

## Описание
На уровне L2 показываем **разворачиваемые единицы** (контейнеры/сервисы) и внешние системы.
Важно: в текущем MVP **физически разворачивается один контейнер** `api`, внутри которого находятся и HTTP-часть, и streaming-контур.

## Диаграмма (L2)

```mermaid
flowchart LR
  OP["Оператор"] -->|HTTP 8000| API["Контейнер api: FastAPI, UI, streaming loop"]

  MISSION["MISSION_DIR: данные миссии, frames, optional annotations"] -->|read-only| API
  RUNTIME["./runtime -> /app/runtime"] -->|read/write| API

  %% Опционально/план
  DB["Postgres"] -.-> API
  S3["S3 Object Storage"] -.-> API
  MON["Prometheus/Grafana"] -.-> API
  AIRFLOW["Airflow DAG: batch-контур, планируется"] -.-> DB
  AIRFLOW -.-> S3
```
