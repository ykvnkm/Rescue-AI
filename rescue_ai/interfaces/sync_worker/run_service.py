"""CLI entry point for the sync-worker (offline profile).

Дренирует локальную таблицу ``replication_outbox`` в удалённый контур
(remote Postgres + remote S3) при наличии связи. Запускается Helm
sub-chart-ом ``rescue-ai-sync-worker`` в namespace ``rescue-ai``
(только когда ``DEPLOYMENT_MODE=offline``).

Sync-worker создаёт **два** S3-клиента:

* ``source_s3_client`` — на локальный MinIO (``settings.storage.s3_*``).
  Используется для GET-а артефактов миссии перед уплоадом в удалённое
  S3. Credentials получаются через Vault Agent из KV-пути
  ``secret/rescue-ai/sync-worker`` (поля ``ARTIFACTS_S3_*`` — те же,
  что у api, чтобы оба пода смотрели в один и тот же бакет).
* ``target_s3_client`` — на удалённое S3
  (``settings.deployment.remote_s3_*``). Используется для PUT-а
  скопированных артефактов. Credentials — поля
  ``DEPLOYMENT_REMOTE_S3_*`` того же KV-пути.

S3-to-S3 копирование происходит в памяти пода sync-worker (GET →
read body → PUT). Альтернативная схема «mc mirror в CronJob» не
используется, потому что нарушает заявленный в дипломе outbox-pattern
и не обеспечивает per-row идемпотентность с привязкой к outbox-таблице.
"""

from __future__ import annotations

import logging
from typing import Any

from rescue_ai.config import get_settings
from rescue_ai.infrastructure.postgres_connection import (
    PostgresDatabase,
    wait_for_postgres,
)
from rescue_ai.infrastructure.sync.remote_sync_target import RemoteSyncTargetAdapter
from rescue_ai.infrastructure.sync.sync_outbox_repository import (
    PostgresSyncOutboxRepository,
)
from rescue_ai.infrastructure.sync.sync_worker import SyncWorker, SyncWorkerConfig


def _build_target_s3_client(deployment) -> Any:
    """Boto3 клиент к удалённому S3 (Yandex Object Storage и пр.).

    Импорт boto3 ленивый — это держит cold-start sync-worker'а быстрым,
    когда S3-репликация artifact'ов не происходит (нет связи).
    """
    import boto3  # noqa: PLC0415

    return boto3.client(
        "s3",
        endpoint_url=deployment.remote_s3_endpoint or None,
        region_name=deployment.remote_s3_region,
        aws_access_key_id=deployment.remote_s3_access_key_id,
        aws_secret_access_key=deployment.remote_s3_secret_access_key,
    )


def _build_source_s3_client(storage) -> Any:
    """Boto3 клиент к локальному MinIO станции.

    Использует те же ``ARTIFACTS_S3_*`` credentials, что у api, —
    выписаны Vault'ом в KV-путь ``secret/rescue-ai/sync-worker``
    (см. ``scripts/security/vault_bootstrap.sh``, флаг
    ``ENABLE_LOCAL_INFRA=true``).
    """
    import boto3  # noqa: PLC0415

    return boto3.client(
        "s3",
        endpoint_url=storage.s3_endpoint or None,
        region_name=storage.s3_region,
        aws_access_key_id=storage.s3_access_key_id,
        aws_secret_access_key=storage.s3_secret_access_key,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    deployment = settings.deployment
    deployment_mode = str(getattr(deployment, "mode", "cloud"))
    if deployment_mode != "offline":
        raise SystemExit(
            "sync-worker requires DEPLOYMENT_MODE=offline; got " f"{deployment_mode}"
        )

    local_db = PostgresDatabase(settings.database.dsn)
    wait_for_postgres(settings.database.dsn, timeout_sec=60.0)

    remote_db = PostgresDatabase(str(getattr(deployment, "remote_db_dsn", "")))
    target_s3 = _build_target_s3_client(deployment)

    # Source-клиент инициализируется только если у settings.storage
    # есть endpoint — иначе S3-репликация артефактов остаётся выключенной,
    # sync-worker реплицирует только DB-метаданные. Полезно для миграции
    # старых станций: после первого rollout новой версии можно отдельно
    # включить S3-копирование, добавив ARTIFACTS_S3_* в Vault KV.
    source_s3 = (
        _build_source_s3_client(settings.storage)
        if settings.storage.s3_endpoint
        else None
    )

    outbox = PostgresSyncOutboxRepository(local_db)
    target = RemoteSyncTargetAdapter(
        remote_db,
        target_s3,
        source_s3_client=source_s3,
    )
    worker = SyncWorker(
        outbox=outbox,
        target=target,
        config=SyncWorkerConfig(
            batch_size=int(getattr(deployment, "sync_batch_size", 50)),
            interval_sec=float(getattr(deployment, "sync_interval_sec", 10.0)),
            max_attempts=int(getattr(deployment, "sync_max_attempts", 10)),
            processing_timeout_sec=float(
                getattr(deployment, "sync_processing_timeout_sec", 120.0)
            ),
        ),
    )
    worker.run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
