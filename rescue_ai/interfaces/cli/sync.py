"""Sync worker entry point."""

from __future__ import annotations

import logging

from rescue_ai.config import get_settings
from rescue_ai.infrastructure.postgres_connection import (
    PostgresDatabase,
    wait_for_postgres,
)
from rescue_ai.infrastructure.sync_outbox_repository import (
    PostgresSyncOutboxRepository,
)
from rescue_ai.infrastructure.sync_worker import SyncWorker


def main() -> None:
    """Start the sync worker process."""
    settings = get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.app.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger(__name__)

    if not settings.sync.enabled:
        logger.error("SYNC_ENABLED is not set to true, exiting")
        return

    dsn = settings.database.dsn
    if not dsn:
        raise RuntimeError("DB_DSN is required for sync worker (local Postgres)")

    logger.info("Waiting for local Postgres...")
    wait_for_postgres(dsn, timeout_sec=settings.api.postgres_ready_timeout_sec)

    db = PostgresDatabase(dsn=dsn)
    outbox = PostgresSyncOutboxRepository(db)
    worker = SyncWorker(outbox=outbox, settings=settings)

    logger.info("Sync worker starting main loop")
    worker.run_forever()


if __name__ == "__main__":
    main()
