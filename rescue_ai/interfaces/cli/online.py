"""Online API server entry point with optional Postgres bootstrap."""

from __future__ import annotations

import os

import uvicorn

from rescue_ai.config import get_settings
from rescue_ai.infrastructure.postgres_connection import (
    ensure_schema,
    resolve_postgres_dsn,
    wait_for_postgres,
)


def main() -> None:
    """Start the API server, bootstrapping Postgres if configured."""
    settings = get_settings()
    _prepare_postgres_backend()
    uvicorn.run(
        "rescue_ai.interfaces.api.app:app",
        host=settings.app.host,
        port=settings.app.port,
    )


def _prepare_postgres_backend() -> None:
    """Wait for Postgres readiness and create tables if needed."""
    settings = get_settings()
    if settings.app.repository_backend != "postgres":
        return

    try:
        dsn = resolve_postgres_dsn()
    except ValueError as error:
        raise RuntimeError(f"Invalid Postgres configuration: {error}") from error

    if not dsn:
        raise RuntimeError(
            "Postgres backend requires APP_POSTGRES_DSN or "
            "APP_POSTGRES_HOST/PORT/DB/USER/PASSWORD"
        )

    os.environ["APP_POSTGRES_DSN"] = dsn

    wait_for_postgres(dsn, timeout_sec=settings.app.postgres_ready_timeout_sec)
    ensure_schema(dsn)


if __name__ == "__main__":
    main()
