"""FastAPI application factory."""

from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.responses import Response
from yaml import safe_dump

from rescue_ai.application.metrics import HTTP_REQUEST_DURATION_SECONDS, render_latest
from rescue_ai.interfaces.api.routes import router

app = FastAPI(
    title="Rescue-AI",
    description=(
        "ML-powered aerial search & rescue system. "
        "Processes drone video frames in real-time, detects people "
        "using YOLOv8, and generates alerts for rescue operators."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)
app.include_router(router)


@app.middleware("http")
async def _record_http_duration(request: Request, call_next):
    """Фиксировать время обработки запроса в Prometheus-гистограмме."""
    start = perf_counter()
    response = await call_next(request)
    elapsed = perf_counter() - start
    # Используем шаблон маршрута (а не конкретный путь), чтобы
    # ограничить кардинальность метрики `route`.
    route = request.scope.get("route")
    route_path = getattr(route, "path", request.url.path)
    HTTP_REQUEST_DURATION_SECONDS.labels(
        method=request.method,
        route=route_path,
        status=str(response.status_code),
    ).observe(elapsed)
    return response


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    """Экспорт всех Prometheus-метрик в формате text-exposition."""
    payload, content_type = render_latest()
    return Response(content=payload, media_type=content_type)


@app.get("/openapi.yaml", include_in_schema=False)
def openapi_yaml() -> Response:
    """Expose OpenAPI schema in YAML format."""
    payload = safe_dump(app.openapi(), sort_keys=False, allow_unicode=False)
    return Response(content=payload, media_type="application/yaml")
