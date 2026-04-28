"""CLI entry point for the sync-worker (hybrid profile).

Started by ``infra/offline/docker-compose.offline.yml`` under the
``hybrid`` compose profile.
"""

from __future__ import annotations

import logging

from rescue_ai.config import get_settings
from rescue_ai.infrastructure.postgres_connection import (
    PostgresDatabase,
    wait_for_postgres,
)
from rescue_ai.infrastructure.sync.remote_sync_target import (
    RemoteSyncTargetAdapter,
)
from rescue_ai.infrastructure.sync.sync_outbox_repository import (
    PostgresSyncOutboxRepository,
)
from rescue_ai.infrastructure.sync.sync_worker import (
    SyncWorker,
    SyncWorkerConfig,
)


def _build_s3_client(deployment) -> object:
    import boto3  # local import — boto3 is heavy and only needed in hybrid

    return boto3.client(
        "s3",
        endpoint_url=deployment.remote_s3_endpoint or None,
        region_name=deployment.remote_s3_region,
        aws_access_key_id=deployment.remote_s3_access_key_id,
        aws_secret_access_key=deployment.remote_s3_secret_access_key,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if settings.deployment.mode != "hybrid":
        raise SystemExit(
            "sync-worker requires DEPLOYMENT_MODE=hybrid; got "
            f"{settings.deployment.mode}"
        )

    local_db = PostgresDatabase(settings.database.dsn)
    wait_for_postgres(settings.database.dsn, timeout_sec=60.0)

    remote_db = PostgresDatabase(settings.deployment.remote_db_dsn)
    s3_client = _build_s3_client(settings.deployment)

    outbox = PostgresSyncOutboxRepository(local_db)
    target = RemoteSyncTargetAdapter(remote_db, s3_client)
    worker = SyncWorker(
        outbox=outbox,
        target=target,
        config=SyncWorkerConfig(
            batch_size=settings.deployment.sync_batch_size,
            interval_sec=settings.deployment.sync_interval_sec,
            max_attempts=settings.deployment.sync_max_attempts,
            processing_timeout_sec=settings.deployment.sync_processing_timeout_sec,
        ),
    )
    worker.run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
