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
RUN uv sync --frozen --no-dev --extra inference --extra batch
ENV PATH="/app/.venv/bin:$PATH"

COPY db_migrations ./db_migrations
COPY alembic.ini ./alembic.ini
COPY config.py ./config.py
COPY configs ./configs
COPY libs ./libs
COPY services ./services
RUN mkdir -p /app/runtime

EXPOSE 8000

CMD ["python", "-m", "services.api_gateway.run"]
