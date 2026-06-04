# syntax=docker/dockerfile:1.7
#
# Rescue-AI multi-stage Dockerfile.
#
# Один Dockerfile собирает шесть независимых образов микросервисов:
#
#   api              — REST + UI + pilot/auto оркестрация миссии
#   detection        — HTTP-обёртка над YOLO (pt / ncnn)
#   nav-engine       — HTTP-обёртка над NavigationEngine (PnP + LK)
#   sync-worker      — outbox → remote Postgres + remote S3 (offline)
#   batch-worker     — ML-стадии (prepare/evaluate/publish), запускаются
#                      Airflow'ом через KubernetesPodOperator
#   batch-exporter   — Postgres → Prometheus метрики batch-прогона
#
# Сборка конкретной цели:
#
#   docker build -t rescue-ai-detection:local --target detection .
#
# Airflow scheduler/webserver/worker используют стоковый образ
# apache/airflow:2.9.3 (не пересобирается здесь, см. helm values).

# ── 1. Базовые слои ────────────────────────────────────────────────
#
# builder-base держит lock-файл + uv. На нём строится по одной
# `uv sync` стадии для каждого сервиса; кеш слоёв uv общий благодаря
# `--mount=type=cache`.
FROM python:3.12-slim AS builder-base
WORKDIR /app
ENV UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./

# runtime-base даёт OS-зависимости, общие для всех Python-сервисов
# (ffmpeg и libgl нужны opencv + ultralytics; для sync-worker и
# batch-exporter они избыточны, но держать единый базовый слой
# проще и каноничнее).
FROM python:3.12-slim AS runtime-base
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 10001 appuser

# ── 2. Builder-стадии (по одной на сервис) ─────────────────────────
#
# Каждый builder ставит ТОЛЬКО зависимости своего extra → .venv
# конкретного образа не содержит лишнего.

FROM builder-base AS builder-api
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra api

FROM builder-base AS builder-detection
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra detection

FROM builder-base AS builder-nav-engine
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra nav-engine

FROM builder-base AS builder-sync-worker
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra sync-worker

FROM builder-base AS builder-batch-worker
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra batch-worker

FROM builder-base AS builder-batch-exporter
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra batch-exporter

# ── 3. Runtime-стадии (по одной на сервис) ─────────────────────────
#
# Каждая копирует ровно свой .venv + общий код приложения и ставит
# CMD на entry point этого сервиса. Helm-чарты могут переопределять
# command, но локальный `docker run rescue-ai-detection:local` уже
# поднимает правильный процесс.

FROM runtime-base AS api
COPY --from=builder-api /app/.venv /app/.venv
COPY configs ./configs
COPY rescue_ai ./rescue_ai
COPY scripts ./scripts
COPY infra/postgres/init ./infra/postgres/init
RUN mkdir -p /app/runtime/models && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
CMD ["python", "-m", "rescue_ai.interfaces.cli.online"]

FROM runtime-base AS detection
COPY --from=builder-detection /app/.venv /app/.venv
COPY configs ./configs
COPY rescue_ai ./rescue_ai
COPY scripts ./scripts
RUN mkdir -p /app/runtime/models && chown -R appuser:appuser /app
USER appuser
EXPOSE 8002
CMD ["python", "-m", "rescue_ai.interfaces.detection.run_service"]

FROM runtime-base AS nav-engine
COPY --from=builder-nav-engine /app/.venv /app/.venv
COPY rescue_ai ./rescue_ai
RUN chown -R appuser:appuser /app
USER appuser
EXPOSE 8001
CMD ["python", "-m", "rescue_ai.interfaces.nav_engine.run_service"]

FROM runtime-base AS sync-worker
COPY --from=builder-sync-worker /app/.venv /app/.venv
COPY rescue_ai ./rescue_ai
RUN chown -R appuser:appuser /app
USER appuser
CMD ["python", "-m", "rescue_ai.interfaces.sync_worker.run_service"]

FROM runtime-base AS batch-worker
COPY --from=builder-batch-worker /app/.venv /app/.venv
COPY configs ./configs
COPY rescue_ai ./rescue_ai
COPY scripts ./scripts
COPY infra/postgres/init ./infra/postgres/init
RUN mkdir -p /app/runtime/models && chown -R appuser:appuser /app
USER appuser
CMD ["python", "-m", "rescue_ai.interfaces.cli.batch"]

FROM runtime-base AS batch-exporter
COPY --from=builder-batch-exporter /app/.venv /app/.venv
COPY rescue_ai ./rescue_ai
RUN chown -R appuser:appuser /app
USER appuser
EXPOSE 8003
CMD ["python", "-m", "rescue_ai.interfaces.batch_exporter.run_service"]
