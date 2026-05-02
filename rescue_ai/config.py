"""Centralized runtime settings for Rescue-AI."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DeploymentMode = Literal["cloud", "offline", "hybrid"]
TlsMode = Literal["off", "mtls"]


class BaseEnvSettings(BaseSettings):
    """Base settings class with .env file support."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class AppSettings(BaseEnvSettings):
    """Application-level runtime settings."""

    env: str = Field(default="dev", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="APP_LOG_LEVEL")
    service_version: str = Field(default="dev", alias="SERVICE_VERSION")


class ApiSettings(BaseEnvSettings):
    """HTTP server binding and startup settings."""

    host: str = Field(default="0.0.0.0", alias="APP_HOST")
    port: int = Field(default=8000, alias="APP_PORT")
    postgres_ready_timeout_sec: float = Field(
        default=30.0,
        alias="APP_POSTGRES_READY_TIMEOUT_SEC",
    )


class DatabaseSettings(BaseEnvSettings):
    """PostgreSQL connection settings."""

    dsn: str = Field(default="", alias="DB_DSN")


class StorageSettings(BaseEnvSettings):
    """S3-compatible artifact storage credentials and paths."""

    s3_endpoint: str = Field(default="", alias="ARTIFACTS_S3_ENDPOINT")
    s3_region: str = Field(default="ru-central1", alias="ARTIFACTS_S3_REGION")
    s3_access_key_id: str = Field(default="", alias="ARTIFACTS_S3_ACCESS_KEY_ID")
    s3_secret_access_key: str = Field(
        default="", alias="ARTIFACTS_S3_SECRET_ACCESS_KEY"
    )
    s3_bucket: str = Field(default="", alias="ARTIFACTS_S3_BUCKET")
    s3_prefix: str = Field(default="missions", alias="ARTIFACTS_S3_PREFIX")


class RpiSettings(BaseEnvSettings):
    """Raspberry Pi video source connection settings."""

    base_url: str = Field(default="", alias="RPI_BASE_URL")
    missions_dir: str = Field(default="", alias="RPI_MISSIONS_DIR")
    rtsp_port: int = Field(default=0, alias="RPI_RTSP_PORT")
    rtsp_path_prefix: str = Field(default="live", alias="RPI_RTSP_PATH_PREFIX")
    timeout_sec: float = Field(default=10.0, alias="RPI_TIMEOUT_SEC")


class DetectionSettings(BaseEnvSettings):
    """Detection inference timeout settings.

    ``service_url`` — опциональный URL отдельного rescue-ai-detection
    сервиса (ADR-0008 §1). Если задан, API использует HTTP-адаптер
    ``HttpDetector``; если пуст — тот же in-process YoloDetector
    (поведение до P3.A).
    """

    http_timeout_sec: float = Field(default=1.0, alias="DETECTION_HTTP_TIMEOUT_SEC")
    service_url: str = Field(default="", alias="DETECTOR_URL")
    service_timeout_sec: float = Field(
        default=5.0, alias="DETECTOR_TIMEOUT_SEC"
    )


class NavigationServiceSettings(BaseEnvSettings):
    """Optional out-of-process navigation engine (ADR-0008 §1).

    Когда ``service_url`` задан, API подключает ``HttpNavigationEngine``
    вместо локального ``NavigationEngine``; иначе монолит как раньше.
    """

    service_url: str = Field(default="", alias="NAV_ENGINE_URL")
    service_timeout_sec: float = Field(
        default=5.0, alias="NAV_ENGINE_TIMEOUT_SEC"
    )


class UploadSettings(BaseEnvSettings):
    """Local storage for UI-uploaded video files (stand-mode auto sessions)."""

    uploads_dir: str = Field(
        default="/tmp/rescue-ai/uploads",
        alias="UPLOAD_DIR",
    )
    max_upload_mb: int = Field(default=512, alias="UPLOAD_MAX_MB")


class AutoStreamSettings(BaseEnvSettings):
    """Auto-mode WebSocket stream encoding defaults."""

    ws_jpeg_quality: int = Field(default=55, alias="AUTO_WS_JPEG_QUALITY")
    ws_max_width: int = Field(default=640, alias="AUTO_WS_MAX_WIDTH")
    ws_emit_max_fps: float = Field(default=8.0, alias="AUTO_WS_EMIT_MAX_FPS")
    save_video_dir: str = Field(
        default="artifacts/auto_recordings",
        alias="AUTO_SAVE_VIDEO_DIR",
    )


class DeploymentSettings(BaseEnvSettings):
    """Deployment profile (cloud / offline / hybrid).

    See ADR-0007. Selects which Postgres/S3 endpoints are authoritative
    and whether the transactional outbox + sync-worker are enabled.
    The application code is identical across modes; only DSNs and the
    enable flag differ.
    """

    mode: DeploymentMode = Field(default="cloud", alias="DEPLOYMENT_MODE")

    # Remote (cloud) targets — used directly in `cloud`, used as the
    # sync target by the sync-worker in `hybrid`.
    remote_db_dsn: str = Field(default="", alias="DEPLOYMENT_REMOTE_DB_DSN")
    remote_s3_endpoint: str = Field(default="", alias="DEPLOYMENT_REMOTE_S3_ENDPOINT")
    remote_s3_region: str = Field(
        default="ru-central1", alias="DEPLOYMENT_REMOTE_S3_REGION"
    )
    remote_s3_access_key_id: str = Field(
        default="", alias="DEPLOYMENT_REMOTE_S3_ACCESS_KEY_ID"
    )
    remote_s3_secret_access_key: str = Field(
        default="", alias="DEPLOYMENT_REMOTE_S3_SECRET_ACCESS_KEY"
    )
    remote_s3_bucket: str = Field(default="", alias="DEPLOYMENT_REMOTE_S3_BUCKET")

    # Sync-worker tuning (hybrid only).
    sync_batch_size: int = Field(default=50, alias="DEPLOYMENT_SYNC_BATCH_SIZE")
    sync_interval_sec: float = Field(default=10.0, alias="DEPLOYMENT_SYNC_INTERVAL_SEC")
    sync_max_attempts: int = Field(default=10, alias="DEPLOYMENT_SYNC_MAX_ATTEMPTS")
    sync_processing_timeout_sec: float = Field(
        default=120.0, alias="DEPLOYMENT_SYNC_PROCESSING_TIMEOUT_SEC"
    )

    @property
    def is_offline_first(self) -> bool:
        """Local Postgres/MinIO is the primary store (offline or hybrid)."""
        return self.mode in ("offline", "hybrid")

    @property
    def outbox_enabled(self) -> bool:
        """Hybrid is the only mode that produces outbox rows."""
        return self.mode == "hybrid"


class SecuritySettings(BaseEnvSettings):
    """Transport security (mTLS for the RPi link).

    See ADR-0007. ``off`` is dev-only; offline/hybrid profiles must run
    with ``mtls`` because the RPi link traverses an untrusted local
    network with no public tunnel in front of it.
    """

    tls_mode: TlsMode = Field(default="off", alias="TLS_MODE")
    ca_cert_path: str = Field(default="", alias="TLS_CA_CERT_PATH")
    client_cert_path: str = Field(default="", alias="TLS_CLIENT_CERT_PATH")
    client_key_path: str = Field(default="", alias="TLS_CLIENT_KEY_PATH")

    @model_validator(mode="after")
    def _validate_paths(self) -> "SecuritySettings":
        if self.tls_mode == "mtls":
            missing = [
                name
                for name, value in (
                    ("TLS_CA_CERT_PATH", self.ca_cert_path),
                    ("TLS_CLIENT_CERT_PATH", self.client_cert_path),
                    ("TLS_CLIENT_KEY_PATH", self.client_key_path),
                )
                if not value
            ]
            if missing:
                raise ValueError("TLS_MODE=mtls requires: " + ", ".join(missing))
        return self


class Settings(BaseSettings):
    """Aggregated application settings."""

    app: AppSettings
    api: ApiSettings
    database: DatabaseSettings
    storage: StorageSettings
    rpi: RpiSettings
    detection: DetectionSettings
    uploads: UploadSettings
    auto_stream: AutoStreamSettings
    # Defaults keep cloud-mode wiring valid for callers that still
    # construct ``Settings(app=..., api=..., ...)`` without the new
    # sub-settings (legacy tests, scripts).
    deployment: DeploymentSettings = Field(default_factory=DeploymentSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    navigation_service: NavigationServiceSettings = Field(
        default_factory=NavigationServiceSettings
    )

    @model_validator(mode="after")
    def _validate_profile(self) -> "Settings":
        deployment_mode = str(getattr(self.deployment, "mode", "cloud"))
        tls_mode = str(getattr(self.security, "tls_mode", "off"))
        app_env = str(getattr(self.app, "env", "dev"))
        # ADR-0007 §4: non-cloud profiles must run mTLS — the RPi link
        # in the field is not protected by a public tunnel.
        if (
            deployment_mode in ("offline", "hybrid")
            and tls_mode == "off"
            and app_env != "dev"
        ):
            raise ValueError(
                "TLS_MODE=off is not allowed when DEPLOYMENT_MODE="
                f"{deployment_mode} outside dev"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        app=AppSettings(),
        api=ApiSettings(),
        database=DatabaseSettings(),
        storage=StorageSettings(),
        rpi=RpiSettings(),
        detection=DetectionSettings(),
        uploads=UploadSettings(),
        auto_stream=AutoStreamSettings(),
        deployment=DeploymentSettings(),
        security=SecuritySettings(),
        navigation_service=NavigationServiceSettings(),
    )
