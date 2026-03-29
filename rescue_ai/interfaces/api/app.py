"""FastAPI application factory."""

from fastapi import FastAPI
from fastapi.responses import Response
from yaml import safe_dump

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


@app.get("/openapi.yaml", include_in_schema=False)
def openapi_yaml() -> Response:
    """Expose OpenAPI schema in YAML format."""
    payload = safe_dump(app.openapi(), sort_keys=False, allow_unicode=False)
    return Response(content=payload, media_type="application/yaml")
