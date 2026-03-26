"""Online API server entry point with optional Postgres bootstrap."""

from __future__ import annotations

import uvicorn

from rescue_ai.config import get_settings
from rescue_ai.infrastructure.postgres_connection import (
    ensure_schema,
    wait_for_postgres,
)


def main() -> None:
    """Start the API server, bootstrapping Postgres if configured."""
    settings = get_settings()
    _prepare_postgres_backend()
    uvicorn.run(
        "rescue_ai.interfaces.api.app:app",
        host=settings.api.host,
        port=settings.api.port,
    )


def _prepare_postgres_backend() -> None:
    """Wait for Postgres readiness and create tables if needed."""
    settings = get_settings()
    if settings.api.repository_backend != "postgres":
        return

    dsn = settings.database.dsn
    if not dsn:
        raise RuntimeError("Postgres backend requires DB_DSN")

    wait_for_postgres(dsn, timeout_sec=settings.api.postgres_ready_timeout_sec)
    ensure_schema(dsn)


if __name__ == "__main__":
    main()
