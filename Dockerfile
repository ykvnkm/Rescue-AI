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
RUN uv sync --frozen --no-dev --extra inference
RUN pip install --no-cache-dir boto3==1.35.99 psycopg[binary]==3.2.3

COPY configs ./configs
COPY libs ./libs
COPY services ./services
RUN mkdir -p /app/runtime

EXPOSE 8000

CMD ["uv", "run", "python", "-m", "uvicorn", "services.api_gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
