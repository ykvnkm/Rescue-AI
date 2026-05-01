"""Tests for DeploymentSettings / SecuritySettings (ADR-0007)."""

from __future__ import annotations

import pytest

from rescue_ai.config import (
    ApiSettings,
    AppSettings,
    AutoStreamSettings,
    DatabaseSettings,
    DeploymentSettings,
    DetectionSettings,
    RpiSettings,
    SecuritySettings,
    Settings,
    StorageSettings,
    UploadSettings,
)


def _make(
    *,
    deployment: DeploymentSettings,
    security: SecuritySettings,
    env: str = "dev",
) -> Settings:
    return Settings(
        app=AppSettings(APP_ENV=env),
        api=ApiSettings(),
        database=DatabaseSettings(DB_DSN="postgresql://x/y"),
        storage=StorageSettings(),
        rpi=RpiSettings(),
        detection=DetectionSettings(),
        uploads=UploadSettings(),
        auto_stream=AutoStreamSettings(),
        deployment=deployment,
        security=security,
    )


def test_default_mode_is_cloud_and_outbox_disabled() -> None:
    deployment = DeploymentSettings()
    assert deployment.mode == "cloud"
    assert deployment.outbox_enabled is False
    assert deployment.is_offline_first is False


def test_offline_and_hybrid_are_offline_first() -> None:
    assert DeploymentSettings(DEPLOYMENT_MODE="offline").is_offline_first is True
    assert DeploymentSettings(DEPLOYMENT_MODE="hybrid").is_offline_first is True


def test_outbox_only_enabled_in_hybrid() -> None:
    assert DeploymentSettings(DEPLOYMENT_MODE="offline").outbox_enabled is False
    assert DeploymentSettings(DEPLOYMENT_MODE="hybrid").outbox_enabled is True


def test_security_mtls_requires_paths() -> None:
    with pytest.raises(ValueError):
        SecuritySettings(TLS_MODE="mtls")


def test_security_mtls_accepts_paths() -> None:
    sec = SecuritySettings(
        TLS_MODE="mtls",
        TLS_CA_CERT_PATH="/etc/ca.crt",
        TLS_CLIENT_CERT_PATH="/etc/client.crt",
        TLS_CLIENT_KEY_PATH="/etc/client.key",
    )
    assert sec.tls_mode == "mtls"


def test_offline_profile_in_dev_allows_tls_off() -> None:
    settings = _make(
        deployment=DeploymentSettings(DEPLOYMENT_MODE="offline"),
        security=SecuritySettings(TLS_MODE="off"),
        env="dev",
    )
    assert str(getattr(settings.deployment, "mode", "")) == "offline"


def test_offline_profile_outside_dev_rejects_tls_off() -> None:
    with pytest.raises(ValueError):
        _make(
            deployment=DeploymentSettings(DEPLOYMENT_MODE="offline"),
            security=SecuritySettings(TLS_MODE="off"),
            env="field",
        )


def test_cloud_profile_allows_tls_off_anywhere() -> None:
    # Cloud profile keeps current behaviour — RPi link sits behind a
    # public tunnel, mTLS is optional.
    settings = _make(
        deployment=DeploymentSettings(DEPLOYMENT_MODE="cloud"),
        security=SecuritySettings(TLS_MODE="off"),
        env="prod",
    )
    assert str(getattr(settings.security, "tls_mode", "")) == "off"
