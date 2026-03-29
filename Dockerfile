# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra inference --extra batch

ENV PATH="/app/.venv/bin:$PATH"

COPY configs ./configs
COPY rescue_ai ./rescue_ai
COPY scripts ./scripts
RUN mkdir -p /app/runtime

FROM base AS online
EXPOSE 8000
CMD ["uv", "run", "python", "-m", "rescue_ai.interfaces.cli.online"]

FROM base AS batch
ENTRYPOINT ["uv", "run", "python", "-m", "rescue_ai.interfaces.cli.batch"]
