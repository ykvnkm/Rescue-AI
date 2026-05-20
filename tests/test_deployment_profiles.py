"""Profile-level tests: cloud stays plain, offline wires up correctly.

The real wiring lives in `interfaces/api/dependencies.py` and is built
from `Settings`. These tests stay at the configuration boundary so they
don't drag in psycopg / boto3.
"""

from __future__ import annotations

from rescue_ai.config import (
    DatabaseSettings,
    DeploymentSettings,
    SecuritySettings,
    StorageSettings,
)


def test_cloud_profile_uses_remote_dsn_and_remote_s3() -> None:
    """Cloud is unchanged: DB_DSN / ARTIFACTS_S3_* are the source of truth."""
    deployment = DeploymentSettings(DEPLOYMENT_MODE="cloud")
    db = DatabaseSettings(DB_DSN="postgresql://prod-host/rescue_ai")
    s3 = StorageSettings(
        ARTIFACTS_S3_ENDPOINT="https://storage.yandexcloud.net",
        ARTIFACTS_S3_BUCKET="rescue-prod",
        ARTIFACTS_S3_ACCESS_KEY_ID="key",
        ARTIFACTS_S3_SECRET_ACCESS_KEY="secret",
    )

    assert deployment.is_offline_first is False
    assert deployment.outbox_enabled is False
    # The cloud profile reads straight from the legacy fields — no
    # additional remote_* values are required.
    assert str(getattr(db, "dsn", "")).startswith("postgresql://prod-host")
    assert str(getattr(s3, "s3_endpoint", "")).endswith("yandexcloud.net")


def test_offline_profile_enables_outbox_and_local_storage() -> None:
    """Offline profile is the unified ground-station profile.

    Local Postgres and MinIO are the primary store; the sync-worker
    drains the outbox to the remote contour when connectivity is
    available. If connectivity never appears, the outbox simply keeps
    growing — the station stays fully functional in either case.
    """
    deployment = DeploymentSettings(
        DEPLOYMENT_MODE="offline",
        DEPLOYMENT_REMOTE_DB_DSN="postgresql://cloud-host/rescue_ai",
        DEPLOYMENT_REMOTE_S3_ENDPOINT="https://storage.yandexcloud.net",
        DEPLOYMENT_REMOTE_S3_BUCKET="rescue-prod",
        DEPLOYMENT_REMOTE_S3_ACCESS_KEY_ID="key",
        DEPLOYMENT_REMOTE_S3_SECRET_ACCESS_KEY="secret",
    )
    s3 = StorageSettings(
        ARTIFACTS_S3_ENDPOINT="http://minio:9000",
        ARTIFACTS_S3_BUCKET="rescue-artifacts",
        ARTIFACTS_S3_ACCESS_KEY_ID="rescueadmin",
        ARTIFACTS_S3_SECRET_ACCESS_KEY="rescueadmin",
    )

    assert deployment.is_offline_first is True
    assert deployment.outbox_enabled is True
    # Local endpoints are addressed via container hostnames, not Yandex.
    assert "minio" in s3.s3_endpoint
    assert "yandexcloud" not in s3.s3_endpoint
    # Remote sync targets are configured.
    assert str(getattr(deployment, "remote_db_dsn", "")).startswith(
        "postgresql://cloud-host"
    )
    assert deployment.remote_s3_bucket == "rescue-prod"


def test_security_defaults_match_cloud_legacy_behaviour() -> None:
    sec = SecuritySettings()
    assert sec.tls_mode == "off"
    assert sec.ca_cert_path == ""
