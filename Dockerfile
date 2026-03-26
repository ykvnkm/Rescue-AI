# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

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
    set -eux; \
    for attempt in 1 2 3 4 5; do \
      uv sync --frozen --no-dev --extra inference --extra batch && break; \
      if [ "$attempt" -eq 5 ]; then \
        echo "uv sync failed after ${attempt} attempts" >&2; \
        exit 1; \
      fi; \
      echo "uv sync failed on attempt ${attempt}, retrying..." >&2; \
      sleep $((attempt * 5)); \
    done
ENV PATH="/app/.venv/bin:$PATH"

COPY configs ./configs
COPY rescue_ai ./rescue_ai
COPY scripts ./scripts
RUN mkdir -p /app/runtime

EXPOSE 8000

CMD ["uv", "run", "python", "-m", "rescue_ai.interfaces.cli.online"]
