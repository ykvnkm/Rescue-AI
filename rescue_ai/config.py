"""Centralized runtime settings for Rescue-AI."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseEnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class AppSettings(BaseEnvSettings):
    env: str = Field(default="dev", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="APP_LOG_LEVEL")
    service_version: str = Field(default="dev", alias="SERVICE_VERSION")


class ApiSettings(BaseEnvSettings):
    host: str = Field(default="0.0.0.0", alias="APP_HOST")
    port: int = Field(default=8000, alias="APP_PORT")
    postgres_ready_timeout_sec: float = Field(
        default=30.0,
        alias="APP_POSTGRES_READY_TIMEOUT_SEC",
    )


class DatabaseSettings(BaseEnvSettings):
    dsn: str = Field(default="", alias="DB_DSN")


class StorageSettings(BaseEnvSettings):
    s3_endpoint: str = Field(default="", alias="ARTIFACTS_S3_ENDPOINT")
    s3_region: str = Field(default="ru-central1", alias="ARTIFACTS_S3_REGION")
    s3_access_key_id: str = Field(default="", alias="ARTIFACTS_S3_ACCESS_KEY_ID")
    s3_secret_access_key: str = Field(
        default="", alias="ARTIFACTS_S3_SECRET_ACCESS_KEY"
    )
    s3_bucket: str = Field(default="", alias="ARTIFACTS_S3_BUCKET")
    s3_prefix: str = Field(default="missions", alias="ARTIFACTS_S3_PREFIX")


class RpiSettings(BaseEnvSettings):
    base_url: str = Field(default="", alias="RPI_BASE_URL")
    missions_dir: str = Field(default="", alias="RPI_MISSIONS_DIR")
    rtsp_port: int = Field(default=0, alias="RPI_RTSP_PORT")
    rtsp_path_prefix: str = Field(default="live", alias="RPI_RTSP_PATH_PREFIX")
    timeout_sec: float = Field(default=10.0, alias="RPI_TIMEOUT_SEC")


class DetectionSettings(BaseEnvSettings):
    http_timeout_sec: float = Field(default=1.0, alias="DETECTION_HTTP_TIMEOUT_SEC")


class Settings(BaseSettings):
    app: AppSettings
    api: ApiSettings
    database: DatabaseSettings
    storage: StorageSettings
    rpi: RpiSettings
    detection: DetectionSettings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        app=AppSettings(),
        api=ApiSettings(),
        database=DatabaseSettings(),
        storage=StorageSettings(),
        rpi=RpiSettings(),
        detection=DetectionSettings(),
    )
