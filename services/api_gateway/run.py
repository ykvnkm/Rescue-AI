from __future__ import annotations

import os

import uvicorn

from config import config
from libs.infra.postgres import resolve_postgres_dsn, wait_for_postgres


def main() -> None:
    _prepare_postgres_backend()
    uvicorn.run(
        "services.api_gateway.app:app",
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8000")),
    )


def _prepare_postgres_backend() -> None:
    backend = config.get_non_empty("APP_REPOSITORY_BACKEND", default="memory").lower()
    if backend != "postgres":
        return

    dsn = resolve_postgres_dsn()
    if not dsn:
        raise RuntimeError(
            "Postgres backend requires APP_POSTGRES_DSN or "
            "APP_POSTGRES_HOST/PORT/DB/USER/PASSWORD"
        )

    os.environ["APP_POSTGRES_DSN"] = dsn

    timeout_sec = config.get_float("APP_POSTGRES_READY_TIMEOUT_SEC", default=30.0)
    wait_for_postgres(dsn, timeout_sec=timeout_sec)

    if config.get_bool("APP_POSTGRES_AUTO_MIGRATE", default=True):
        from alembic import command
        from alembic.config import Config as AlembicConfig

        alembic_config = AlembicConfig("alembic.ini")
        alembic_config.set_main_option("script_location", "db_migrations")
        command.upgrade(alembic_config, "head")


if __name__ == "__main__":
    main()
