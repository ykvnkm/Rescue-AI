"""Profile-level tests: cloud stays plain, offline/hybrid wire up correctly.

The real wiring lives in `interfaces/api/dependencies.py` and is built
from `Settings`. These tests stay at the configuration boundary so they
don't drag in psycopg / boto3.
"""

from __future__ import annotations

from rescue_ai.config import (
    DeploymentSettings,
    SecuritySettings,
    StorageSettings,
    DatabaseSettings,
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
    assert db.dsn.startswith("postgresql://prod-host")
    assert s3.s3_endpoint.endswith("yandexcloud.net")


def test_offline_profile_points_to_local_postgres_and_minio() -> None:
    deployment = DeploymentSettings(DEPLOYMENT_MODE="offline")
    db = DatabaseSettings(DB_DSN="postgresql://postgres:5432/rescue_ai")
    s3 = StorageSettings(
        ARTIFACTS_S3_ENDPOINT="http://minio:9000",
        ARTIFACTS_S3_BUCKET="rescue-artifacts",
        ARTIFACTS_S3_ACCESS_KEY_ID="rescueadmin",
        ARTIFACTS_S3_SECRET_ACCESS_KEY="rescueadmin",
    )

    assert deployment.is_offline_first is True
    assert deployment.outbox_enabled is False  # offline never syncs out
    # Local endpoints are addressed via container hostnames, not Yandex.
    assert "minio" in s3.s3_endpoint
    assert "yandexcloud" not in s3.s3_endpoint


def test_hybrid_profile_enables_outbox_and_keeps_remote_targets() -> None:
    deployment = DeploymentSettings(
        DEPLOYMENT_MODE="hybrid",
        DEPLOYMENT_REMOTE_DB_DSN="postgresql://cloud-host/rescue_ai",
        DEPLOYMENT_REMOTE_S3_ENDPOINT="https://storage.yandexcloud.net",
        DEPLOYMENT_REMOTE_S3_BUCKET="rescue-prod",
        DEPLOYMENT_REMOTE_S3_ACCESS_KEY_ID="key",
        DEPLOYMENT_REMOTE_S3_SECRET_ACCESS_KEY="secret",
    )

    assert deployment.is_offline_first is True
    assert deployment.outbox_enabled is True
    # Sync-worker has explicit remote targets to drain into.
    assert deployment.remote_db_dsn.startswith("postgresql://cloud-host")
    assert deployment.remote_s3_bucket == "rescue-prod"


def test_security_defaults_match_cloud_legacy_behaviour() -> None:
    sec = SecuritySettings()
    assert sec.tls_mode == "off"
    assert sec.ca_cert_path == ""
