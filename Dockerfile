# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS common

WORKDIR /app
ENV UV_LINK_MODE=copy

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./

FROM common AS online-base
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra inference --extra batch

ENV PATH="/app/.venv/bin:$PATH"

COPY configs ./configs
COPY rescue_ai ./rescue_ai
COPY scripts ./scripts
COPY infra/postgres/init ./infra/postgres/init
RUN mkdir -p /app/runtime

FROM online-base AS online
EXPOSE 8000
CMD ["uv", "run", "python", "-m", "rescue_ai.interfaces.cli.online"]

FROM common AS batch
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra inference --extra batch
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python \
    apache-airflow==2.9.3 \
    apache-airflow-providers-docker==3.11.0

ENV PATH="/app/.venv/bin:$PATH"
ENV AIRFLOW_HOME="/opt/airflow"

COPY configs ./configs
COPY rescue_ai ./rescue_ai
COPY scripts ./scripts
COPY infra/postgres/init ./infra/postgres/init
COPY infra/airflow/dags /opt/airflow/dags
COPY infra/airflow/plugins /opt/airflow/plugins
RUN mkdir -p /app/runtime /opt/airflow/logs

CMD ["uv", "run", "python", "-m", "rescue_ai.interfaces.cli.batch"]
