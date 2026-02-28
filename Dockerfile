FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY libs ./libs
COPY services ./services

EXPOSE 8000

CMD ["uv", "run", "python", "-m", "uvicorn", "services.api_gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
