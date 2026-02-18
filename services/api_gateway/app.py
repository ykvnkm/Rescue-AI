"""API gateway entrypoint."""

from fastapi import FastAPI

app = FastAPI(title="API")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict[str, str]:
    return {"status": "ready"}


@app.get("/version")
def version() -> dict[str, str]:
    return {"version": "0.1.0"}
