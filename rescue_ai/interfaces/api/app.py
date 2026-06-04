"""FastAPI application factory."""

from time import monotonic, perf_counter

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from yaml import safe_dump

from rescue_ai.application.metrics import HTTP_REQUEST_DURATION_SECONDS, render_latest
from rescue_ai.config import get_settings
from rescue_ai.interfaces.api.auth import is_authorized, is_open_path
from rescue_ai.interfaces.api.rate_limit import FixedWindowRateLimiter, client_key
from rescue_ai.interfaces.api.routes import router

_rate_limiter = FixedWindowRateLimiter(get_settings().api.rate_limit_per_min)

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


# Registered last → runs outermost: unauthorized requests are rejected before
# any other middleware or handler work. Disabled (pass-through) when
# ``API_AUTH_TOKEN`` is empty, e.g. offline/dev profiles.
@app.middleware("http")
async def _enforce_bearer_auth(request: Request, call_next):
    """Reject unauthenticated API calls when a shared secret is configured."""
    security = get_settings().security
    expected = getattr(security, "api_auth_token", "")
    if not is_authorized(
        path=request.url.path,
        authorization_header=request.headers.get("authorization"),
        expected_token=expected,
    ):
        return JSONResponse(
            {"detail": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)


@app.middleware("http")
async def _enforce_rate_limit(request: Request, call_next):
    """Cap per-client request rate on the open demo (off when limit <= 0).

    Probes, metrics and the UI shell are exempt so monitoring and page loads
    are never throttled.
    """
    if _rate_limiter.enabled and not is_open_path(request.url.path):
        key = client_key(
            forwarded_for=request.headers.get("x-forwarded-for"),
            peer=request.client.host if request.client else None,
        )
        if not _rate_limiter.allow(key, monotonic()):
            return JSONResponse(
                {"detail": "Too Many Requests"},
                status_code=429,
                headers={"Retry-After": "60"},
            )
    return await call_next(request)


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
