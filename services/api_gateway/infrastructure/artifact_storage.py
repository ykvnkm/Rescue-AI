from __future__ import annotations

import json
import mimetypes
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from shutil import copy2
from urllib.parse import urlparse

from config import config
from libs.core.application.contracts import ArtifactBlob, ArtifactStorage

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:
    boto3 = None
    BotoCoreError = Exception
    ClientError = Exception


S3_OPERATION_ERRORS = (ClientError, BotoCoreError, OSError)


@dataclass(frozen=True)
class S3ArtifactBackendSettings:
    """Settings for private S3 artifact backend."""

    endpoint: str | None = None
    region: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    bucket: str | None = None
    strict: bool = True

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
class ArtifactStorageSettings:
    """Environment-driven settings for selecting artifact storage adapter."""

    mode: str = "s3"
    local_root: Path = Path("runtime/artifacts")
    s3: S3ArtifactBackendSettings = S3ArtifactBackendSettings()

    @classmethod
    def from_env(cls) -> ArtifactStorageSettings:
        return cls(
            mode=_normalize_mode(config.get("ARTIFACTS_MODE")),
            local_root=Path(
                _clean_env_value(config.get("ARTIFACTS_LOCAL_ROOT"))
                or "runtime/artifacts"
            ),
            s3=S3ArtifactBackendSettings(
                endpoint=_clean_env_value(config.get("ARTIFACTS_S3_ENDPOINT")),
                region=_clean_env_value(config.get("ARTIFACTS_S3_REGION")),
                access_key_id=_clean_env_value(
                    config.get("ARTIFACTS_S3_ACCESS_KEY_ID")
                ),
                secret_access_key=_clean_env_value(
                    config.get("ARTIFACTS_S3_SECRET_ACCESS_KEY")
                ),
                bucket=_clean_env_value(config.get("ARTIFACTS_S3_BUCKET")),
                strict=_env_bool(config.get("ARTIFACTS_S3_STRICT"), default=True),
            ),
        )


def build_artifact_storage(
    settings: ArtifactStorageSettings | None = None,
) -> ArtifactStorage:
    resolved = settings or ArtifactStorageSettings.from_env()
    local_storage = LocalArtifactStorage(resolved.local_root)

    if resolved.mode != "s3":
        return local_storage
    if not resolved.s3.has_credentials:
        return local_storage
    if not resolved.s3.ready:
        raise RuntimeError(
            "ARTIFACTS_MODE=s3 requires ARTIFACTS_S3_ENDPOINT, ARTIFACTS_S3_REGION, "
            "ARTIFACTS_S3_ACCESS_KEY_ID, ARTIFACTS_S3_SECRET_ACCESS_KEY, "
            "ARTIFACTS_S3_BUCKET"
        )

    return S3ArtifactStorage(
        settings=resolved.s3,
        fallback_storage=local_storage,
    )


class LocalArtifactStorage:
    """Stores artifacts in local filesystem under runtime root."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def store_frame(self, mission_id: str, frame_id: int, source_uri: str) -> str:
        source_path = _local_path_from_uri(source_uri)
        if source_path is None or not source_path.exists() or not source_path.is_file():
            return source_uri

        suffix = source_path.suffix.lower() or ".bin"
        target_path = self._frame_path(
            mission_id=mission_id, frame_id=frame_id, suffix=suffix
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        copy2(source_path, target_path)
        return str(target_path.resolve())

    def load_frame(self, image_uri: str) -> ArtifactBlob | None:
        image_path = _local_path_from_uri(image_uri)
        if image_path is None or not image_path.exists() or not image_path.is_file():
            return None

        media_type, _ = mimetypes.guess_type(image_path.name)
        return ArtifactBlob(
            content=image_path.read_bytes(),
            media_type=media_type or "application/octet-stream",
            filename=image_path.name,
        )

    def save_mission_report(self, mission_id: str, report: dict[str, object]) -> str:
        report_path = self._report_path(mission_id)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(report_path.resolve())

    def load_mission_report(self, mission_id: str) -> dict[str, object] | None:
        report_path = self._report_path(mission_id)
        if not report_path.exists() or not report_path.is_file():
            return None
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _report_path(self, mission_id: str) -> Path:
        return self._root / "missions" / mission_id / "report.json"

    def _frame_path(self, mission_id: str, frame_id: int, suffix: str) -> Path:
        return self._root / "missions" / mission_id / "frames" / f"{frame_id}{suffix}"


class S3ArtifactStorage:
    """Stores artifacts in private S3 bucket with local fallback."""

    def __init__(
        self,
        settings: S3ArtifactBackendSettings,
        fallback_storage: LocalArtifactStorage,
    ) -> None:
        if boto3 is None:
            raise RuntimeError("boto3 is required for ARTIFACTS_MODE=s3")

        self._settings = settings
        self._fallback = fallback_storage
        self._lock = threading.Lock()
        self._pending_frames: dict[str, PendingFrameUpload] = {}
        self._uploads = ThreadPoolExecutor(max_workers=2, thread_name_prefix="artifact")
        self._client = boto3.client(
            "s3",
            endpoint_url=self._settings.endpoint,
            region_name=self._settings.region,
            aws_access_key_id=self._settings.access_key_id,
            aws_secret_access_key=self._settings.secret_access_key,
        )

    def store_frame(self, mission_id: str, frame_id: int, source_uri: str) -> str:
        source_path = _local_path_from_uri(source_uri)
        if source_path is None or not source_path.exists() or not source_path.is_file():
            return source_uri

        suffix = source_path.suffix.lower() or ".bin"
        key = f"missions/{mission_id}/frames/{frame_id}{suffix}"
        s3_uri = f"s3://{self._settings.bucket}/{key}"

        # Keep a local copy so the alert frame can be read immediately
        # while S3 upload is in progress.
        local_uri = self._fallback.store_frame(mission_id, frame_id, source_uri)
        with self._lock:
            self._pending_frames[key] = PendingFrameUpload(fallback_uri=local_uri)

        media_type, _ = mimetypes.guess_type(source_path.name)
        payload = source_path.read_bytes()
        self._uploads.submit(
            self._upload_frame,
            key,
            payload,
            media_type or "application/octet-stream",
        )
        return s3_uri

    def load_frame(self, image_uri: str) -> ArtifactBlob | None:
        parsed = _parse_s3_uri(image_uri)
        if parsed is None:
            return self._fallback.load_frame(image_uri)
        bucket, key = parsed

        with self._lock:
            pending = self._pending_frames.get(key)
        if pending is not None and pending.error is not None and self._settings.strict:
            raise RuntimeError(pending.error)

        try:
            response = self._client.get_object(Bucket=bucket, Key=key)
        except S3_OPERATION_ERRORS as error:
            if pending is not None:
                return self._fallback.load_frame(pending.fallback_uri)
            if _is_missing_s3_object_error(error):
                return None
            if self._settings.strict:
                raise
            return self._fallback.load_frame(image_uri)

        with self._lock:
            self._pending_frames.pop(key, None)
        body = response["Body"].read()
        content_type = (
            response.get("ContentType")
            or mimetypes.guess_type(Path(key).name)[0]
            or "application/octet-stream"
        )
        return ArtifactBlob(
            content=body,
            media_type=content_type,
            filename=Path(key).name or "frame.bin",
        )

    def save_mission_report(self, mission_id: str, report: dict[str, object]) -> str:
        key = self._report_key(mission_id)
        payload = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            self._client.put_object(
                Bucket=self._settings.bucket,
                Key=key,
                Body=payload,
                ContentType="application/json",
            )
        except S3_OPERATION_ERRORS:
            if self._settings.strict:
                raise
            return self._fallback.save_mission_report(mission_id, report)
        return f"s3://{self._settings.bucket}/{key}"

    def load_mission_report(self, mission_id: str) -> dict[str, object] | None:
        key = self._report_key(mission_id)
        try:
            response = self._client.get_object(Bucket=self._settings.bucket, Key=key)
        except S3_OPERATION_ERRORS as error:
            if _is_missing_s3_object_error(error):
                return None
            if self._settings.strict:
                raise
            return self._fallback.load_mission_report(mission_id)

        try:
            payload = json.loads(response["Body"].read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _report_key(self, mission_id: str) -> str:
        return f"missions/{mission_id}/report.json"

    def _upload_frame(self, key: str, payload: bytes, content_type: str) -> None:
        try:
            self._client.put_object(
                Bucket=self._settings.bucket,
                Key=key,
                Body=payload,
                ContentType=content_type,
            )
        except S3_OPERATION_ERRORS as error:
            with self._lock:
                pending = self._pending_frames.get(key)
                if pending is not None:
                    pending.error = (
                        f"Frame upload failed for {key}: "
                        f"{type(error).__name__}: {error}"
                    )
        else:
            with self._lock:
                self._pending_frames.pop(key, None)


def _clean_env_value(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _normalize_mode(mode: str | None) -> str:
    if mode is None:
        return "s3"
    normalized = mode.strip().lower()
    if normalized not in {"local", "s3"}:
        return "local"
    return normalized


def _env_bool(value: str | None, default: bool) -> bool:
    cleaned = _clean_env_value(value)
    if cleaned is None:
        return default
    return cleaned.lower() in {"1", "true", "yes", "on"}


def _local_path_from_uri(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        return Path(parsed.path)
    if "://" in uri:
        return None
    return Path(uri)


def _parse_s3_uri(uri: str) -> tuple[str, str] | None:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        return None
    key = parsed.path.lstrip("/")
    if not key:
        return None
    return parsed.netloc, key


def _is_missing_s3_object_error(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    meta = response.get("ResponseMetadata", {})
    status_code = meta.get("HTTPStatusCode")
    if status_code == 404:
        return True
    error_payload = response.get("Error", {})
    code = str(error_payload.get("Code", ""))
    return code in {"NoSuchKey", "NotFound", "404"}


@dataclass
class PendingFrameUpload:
    """Pending async frame upload with local fallback and optional error."""

    fallback_uri: str
    error: str | None = None
