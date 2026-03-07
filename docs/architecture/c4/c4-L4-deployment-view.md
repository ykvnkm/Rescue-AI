# C4 L4: Развёртывание (Deployment View)

- Статус: Черновик
- Дата: 2026-03-08
- Автор: Максим Яковенко, Провков Иван, Скрыпник Михаил

## Описание
L4 фиксирует, где физически “живут” компоненты в MVP и как это эволюционирует к production.

## Диаграмма (L4)

```mermaid
flowchart LR
  subgraph GS["Наземная станция / ноутбук оператора"]
    DOCKER["Docker: контейнер api, FastAPI + UI + streaming loop"]
    VOL1["MISSION_DIR: данные миссии, read-only"]
    VOL2["./runtime: read/write"]
    VOL1 --- DOCKER
    VOL2 --- DOCKER
  end

  OP["Оператор"] -->|HTTP 8000| DOCKER

  %% План: edge и стрим
  subgraph EDGE["Edge, план"]
    RPI["Raspberry Pi / Edge узел"]
    CAM["Камера / БПЛА"]
    CAM -->|RTSP/UDP| RPI
  end

  RPI -.->|RTSP/UDP план| DOCKER

  %% План: внешние сервисы
  DB["Postgres, план"] -.-> DOCKER
  S3["S3, план"] -.-> DOCKER
  MON["Prometheus/Grafana, план"] -.-> DOCKER
```
