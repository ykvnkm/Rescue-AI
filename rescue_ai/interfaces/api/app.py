"""FastAPI application factory."""

from fastapi import FastAPI

from rescue_ai.interfaces.api.routes import router

app = FastAPI(title="Rescue-AI API")
app.include_router(router)
