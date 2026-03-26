"""Centralized runtime settings for Rescue-AI."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

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
    repository_backend: str = Field(default="memory", alias="APP_REPOSITORY_BACKEND")
    postgres_ready_timeout_sec: float = Field(
        default=30.0,
        alias="APP_POSTGRES_READY_TIMEOUT_SEC",
    )


class DatabaseSettings(BaseEnvSettings):
    dsn: str = Field(default="", alias="DB_DSN")
    batch_dsn: str = Field(default="", alias="BATCH_POSTGRES_DSN")


class StorageSettings(BaseEnvSettings):
    backend: str = Field(default="s3", alias="ARTIFACTS_BACKEND")
    local_root: Path = Field(
        default=Path("runtime/artifacts"),
        alias="ARTIFACTS_LOCAL_ROOT",
    )
    strict: bool = Field(default=True, alias="ARTIFACTS_S3_STRICT")
    s3_endpoint: str = Field(default="", alias="ARTIFACTS_S3_ENDPOINT")
    s3_region: str = Field(default="us-east-1", alias="ARTIFACTS_S3_REGION")
    s3_access_key_id: str = Field(default="", alias="ARTIFACTS_S3_ACCESS_KEY_ID")
    s3_secret_access_key: str = Field(
        default="", alias="ARTIFACTS_S3_SECRET_ACCESS_KEY"
    )
    s3_bucket: str = Field(default="", alias="ARTIFACTS_S3_BUCKET")
    s3_prefix: str = Field(default="batch", alias="ARTIFACTS_S3_PREFIX")


class BatchSettings(BaseEnvSettings):
    runtime_env: str = Field(default="local", alias="BATCH_RUNTIME_ENV")
    status_backend: str = Field(default="", alias="BATCH_STATUS_BACKEND")
    artifact_backend: str = Field(default="", alias="BATCH_ARTIFACT_BACKEND")
    model_version: str = Field(
        default="yolov8n_baseline_multiscale",
        alias="BATCH_MODEL_VERSION",
    )
    code_version: str = Field(default="dev", alias="BATCH_CODE_VERSION")
    source_fps: float = Field(default=6.0, alias="BATCH_SOURCE_FPS")
    status_path: Path = Field(
        default=Path("/opt/airflow/data/status/runs.json"),
        alias="BATCH_STATUS_PATH",
    )
    artifact_root: Path = Field(
        default=Path("/opt/airflow/data/artifacts"),
        alias="BATCH_ARTIFACT_ROOT",
    )
    mission_root: Path = Field(
        default=Path("/opt/airflow/data/missions"),
        alias="BATCH_MISSION_ROOT",
    )
    s3_prefix: str = Field(default="batch", alias="BATCH_S3_PREFIX")


class DetectionSettings(BaseEnvSettings):
    http_timeout_sec: float = Field(default=1.0, alias="DETECTION_HTTP_TIMEOUT_SEC")


class SecretsSettings(BaseEnvSettings):
    online_api_token: str = Field(default="", alias="ONLINE_API_TOKEN")


class Settings(BaseSettings):
    app: AppSettings
    api: ApiSettings
    database: DatabaseSettings
    storage: StorageSettings
    batch: BatchSettings
    detection: DetectionSettings
    secrets: SecretsSettings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        app=AppSettings(),
        api=ApiSettings(),
        database=DatabaseSettings(),
        storage=StorageSettings(),
        batch=BatchSettings(),
        detection=DetectionSettings(),
        secrets=SecretsSettings(),
    )
