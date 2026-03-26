"""Centralized application settings loaded from environment variables.

Every env-var read in the project goes through ``get_settings()``.
Services import the typed settings groups they need instead of
scattering ``os.getenv`` calls across the codebase.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# ── Environment variable helpers ─────────────────────────────────


def _env(name: str, default: str = "") -> str:
    """Read a single env var, stripping whitespace."""
    value = os.getenv(name, "").strip()
    return value or default


def _env_optional(*names: str) -> str | None:
    """Return first non-empty value from several env var names."""
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return None


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    return int(raw)


# ── Settings dataclasses ─────────────────────────────────────────


@dataclass(frozen=True)
class AppSettings:
    """Online API service settings."""

    host: str
    port: int
    repository_backend: str
    postgres_dsn: str | None
    postgres_ready_timeout_sec: float
    service_version: str


@dataclass(frozen=True)
class S3Settings:
    """S3 connection parameters reused by artifact storage and ML pipeline."""

    endpoint: str | None = None
    region: str = "us-east-1"
    access_key_id: str | None = None
    secret_access_key: str | None = None
    bucket: str | None = None
    prefix: str = "batch"

    @property
    def ready(self) -> bool:
        return bool(
            self.endpoint
            and self.region
            and self.access_key_id
            and self.secret_access_key
            and self.bucket
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self.access_key_id and self.secret_access_key)


@dataclass(frozen=True)
class ArtifactSettings:
    """Online artifact storage settings."""

    mode: str = "s3"
    local_root: Path = Path("runtime/artifacts")
    s3: S3Settings = S3Settings()
    strict: bool = True


@dataclass(frozen=True)
class BatchSettings:
    """Batch processing settings."""

    model_version: str = "yolov8n_baseline_multiscale"
    code_version: str = "dev"
    runtime_env: str = "local"
    status_backend: str = "json"
    artifact_backend: str = "local"
    status_path: Path = Path("/opt/airflow/data/status/runs.json")
    artifact_root: Path = Path("/opt/airflow/data/artifacts")
    mission_root: Path = Path("/opt/airflow/data/missions")
    source_fps: float = 6.0
    postgres_dsn: str | None = None
    s3: S3Settings = S3Settings()


@dataclass(frozen=True)
class DetectionSettings:
    """ML detector and inference settings."""

    http_timeout_sec: float = 1.0


@dataclass(frozen=True)
class Settings:
    """Root settings container aggregating all configuration groups."""

    app: AppSettings
    artifacts: ArtifactSettings
    batch: BatchSettings
    detection: DetectionSettings


# ── Settings construction ────────────────────────────────────────


def _resolve_artifact_mode() -> str:
    raw = _env_optional("ARTIFACTS_MODE")
    if raw is None:
        return "s3"
    normalized = raw.lower()
    return normalized if normalized in {"local", "s3"} else "local"


def _resolve_batch_status_backend(runtime_env: str) -> str:
    explicit = _env_optional("BATCH_STATUS_BACKEND")
    if explicit:
        return explicit.lower()
    if runtime_env in {"shared", "stage", "staging", "prod", "production"}:
        return "postgres"
    return "json"


def _resolve_batch_artifact_backend(runtime_env: str) -> str:
    explicit = _env_optional("BATCH_ARTIFACT_BACKEND")
    if explicit:
        return explicit.lower()
    if runtime_env in {"shared", "stage", "staging", "prod", "production"}:
        return "s3"
    return "local"


def _build_batch_s3() -> S3Settings:
    return S3Settings(
        endpoint=_env_optional("BATCH_S3_ENDPOINT", "ARTIFACTS_S3_ENDPOINT"),
        region=_env_optional("BATCH_S3_REGION", "ARTIFACTS_S3_REGION") or "us-east-1",
        access_key_id=_env_optional(
            "BATCH_S3_ACCESS_KEY", "ARTIFACTS_S3_ACCESS_KEY_ID"
        ),
        secret_access_key=_env_optional(
            "BATCH_S3_SECRET_KEY", "ARTIFACTS_S3_SECRET_ACCESS_KEY"
        ),
        bucket=_env_optional("BATCH_S3_BUCKET", "ARTIFACTS_S3_BUCKET"),
        prefix=_env_optional("BATCH_S3_PREFIX", "ARTIFACTS_S3_PREFIX") or "batch",
    )


def _build_artifact_s3() -> S3Settings:
    return S3Settings(
        endpoint=_env_optional("ARTIFACTS_S3_ENDPOINT"),
        region=_env_optional("ARTIFACTS_S3_REGION") or "us-east-1",
        access_key_id=_env_optional("ARTIFACTS_S3_ACCESS_KEY_ID"),
        secret_access_key=_env_optional("ARTIFACTS_S3_SECRET_ACCESS_KEY"),
        bucket=_env_optional("ARTIFACTS_S3_BUCKET"),
        prefix=_env_optional("ARTIFACTS_S3_PREFIX") or "batch",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build and cache all application settings from environment variables."""
    runtime_env = _env("BATCH_RUNTIME_ENV", "local").lower()

    return Settings(
        app=AppSettings(
            host=_env("APP_HOST", "0.0.0.0"),
            port=_env_int("APP_PORT", 8000),
            repository_backend=_env("APP_REPOSITORY_BACKEND", "memory").lower(),
            postgres_dsn=_env_optional("APP_POSTGRES_DSN"),
            postgres_ready_timeout_sec=_env_float(
                "APP_POSTGRES_READY_TIMEOUT_SEC", 30.0
            ),
            service_version=_env("SERVICE_VERSION", "dev"),
        ),
        artifacts=ArtifactSettings(
            mode=_resolve_artifact_mode(),
            local_root=Path(_env("ARTIFACTS_LOCAL_ROOT", "runtime/artifacts")),
            s3=_build_artifact_s3(),
            strict=_env_bool("ARTIFACTS_S3_STRICT", default=True),
        ),
        batch=BatchSettings(
            model_version=_env("BATCH_MODEL_VERSION", "yolov8n_baseline_multiscale"),
            code_version=_env("BATCH_CODE_VERSION", "dev"),
            runtime_env=runtime_env,
            status_backend=_resolve_batch_status_backend(runtime_env),
            artifact_backend=_resolve_batch_artifact_backend(runtime_env),
            status_path=Path(
                _env("BATCH_STATUS_PATH", "/opt/airflow/data/status/runs.json")
            ),
            artifact_root=Path(
                _env("BATCH_ARTIFACT_ROOT", "/opt/airflow/data/artifacts")
            ),
            mission_root=Path(_env("BATCH_MISSION_ROOT", "/opt/airflow/data/missions")),
            source_fps=_env_float("BATCH_SOURCE_FPS", 6.0),
            postgres_dsn=_env_optional("BATCH_POSTGRES_DSN"),
            s3=_build_batch_s3(),
        ),
        detection=DetectionSettings(
            http_timeout_sec=_env_float("DETECTION_HTTP_TIMEOUT_SEC", 1.0),
        ),
    )
