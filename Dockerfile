# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS build-base
WORKDIR /app

ENV UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./

FROM build-base AS builder-online
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra inference --extra postgres

FROM build-base AS builder-batch
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra inference --extra batch --extra airflow

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

FROM runtime-base AS online
COPY --from=builder-online /app/.venv /app/.venv
COPY configs ./configs
COPY rescue_ai ./rescue_ai
COPY scripts ./scripts
COPY infra/postgres/init ./infra/postgres/init

RUN mkdir -p /app/runtime && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000
CMD ["python", "-m", "rescue_ai.interfaces.cli.online"]

FROM runtime-base AS batch
ENV AIRFLOW_HOME="/opt/airflow"

COPY --from=builder-batch /app/.venv /app/.venv
COPY configs ./configs
COPY rescue_ai ./rescue_ai
COPY scripts ./scripts
COPY infra/postgres/init ./infra/postgres/init
COPY infra/airflow/dags /opt/airflow/dags
COPY infra/airflow/plugins /opt/airflow/plugins

RUN mkdir -p /app/runtime /opt/airflow/logs && chown -R appuser:appuser /app /opt/airflow

USER appuser

CMD ["python", "-m", "rescue_ai.interfaces.cli.batch"]
