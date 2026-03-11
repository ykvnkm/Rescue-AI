from __future__ import annotations

import json
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from shutil import copy2
from urllib.parse import urlparse

from libs.core.application.contracts import ArtifactBlob, ArtifactStorage


@dataclass(frozen=True)
class ArtifactStorageSettings:
    """Environment-driven settings for selecting artifact storage adapter."""

    mode: str = "local"
    local_root: Path = Path("runtime/artifacts")
    s3_endpoint: str | None = None
    s3_region: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_bucket: str | None = None

    @classmethod
    def from_env(cls) -> ArtifactStorageSettings:
        return cls(
            mode=_normalize_mode(os.getenv("ARTIFACTS_MODE")),
            local_root=Path(
                _clean_env_value(os.getenv("ARTIFACTS_LOCAL_ROOT"))
                or "runtime/artifacts"
            ),
            s3_endpoint=_clean_env_value(os.getenv("ARTIFACTS_S3_ENDPOINT")),
            s3_region=_clean_env_value(os.getenv("ARTIFACTS_S3_REGION")),
            s3_access_key_id=_clean_env_value(os.getenv("ARTIFACTS_S3_ACCESS_KEY_ID")),
            s3_secret_access_key=_clean_env_value(
                os.getenv("ARTIFACTS_S3_SECRET_ACCESS_KEY")
            ),
            s3_bucket=_clean_env_value(os.getenv("ARTIFACTS_S3_BUCKET")),
        )

    @property
    def s3_ready(self) -> bool:
        return bool(
            self.s3_access_key_id and self.s3_secret_access_key and self.s3_bucket
        )


def build_artifact_storage(
    settings: ArtifactStorageSettings | None = None,
) -> ArtifactStorage:
    resolved = settings or ArtifactStorageSettings.from_env()
    local_storage = LocalArtifactStorage(resolved.local_root)

    if resolved.mode != "s3":
        return local_storage
    if not resolved.s3_ready:
        return local_storage

    try:
        return S3ArtifactStorage(
            bucket=resolved.s3_bucket or "",
            endpoint=resolved.s3_endpoint,
            region=resolved.s3_region,
            access_key_id=resolved.s3_access_key_id or "",
            secret_access_key=resolved.s3_secret_access_key or "",
            fallback_storage=local_storage,
        )
    except RuntimeError:
        return local_storage


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
        bucket: str,
        endpoint: str | None,
        region: str | None,
        access_key_id: str,
        secret_access_key: str,
        fallback_storage: LocalArtifactStorage,
    ) -> None:
        try:
            import boto3  # type: ignore[import-untyped]
        except ImportError as error:
            raise RuntimeError("boto3 is required for ARTIFACTS_MODE=s3") from error

        self._bucket = bucket
        self._fallback = fallback_storage
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )

    def store_frame(self, mission_id: str, frame_id: int, source_uri: str) -> str:
        source_path = _local_path_from_uri(source_uri)
        if source_path is None or not source_path.exists() or not source_path.is_file():
            return source_uri

        suffix = source_path.suffix.lower() or ".bin"
        key = f"missions/{mission_id}/frames/{frame_id}{suffix}"
        media_type, _ = mimetypes.guess_type(source_path.name)
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=source_path.read_bytes(),
                ContentType=media_type or "application/octet-stream",
            )
        except Exception:
            return self._fallback.store_frame(mission_id, frame_id, source_uri)

        return f"s3://{self._bucket}/{key}"

    def load_frame(self, image_uri: str) -> ArtifactBlob | None:
        parsed = _parse_s3_uri(image_uri)
        if parsed is None:
            return self._fallback.load_frame(image_uri)
        bucket, key = parsed

        try:
            response = self._client.get_object(Bucket=bucket, Key=key)
        except Exception:
            return self._fallback.load_frame(image_uri)

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
                Bucket=self._bucket,
                Key=key,
                Body=payload,
                ContentType="application/json",
            )
        except Exception:
            return self._fallback.save_mission_report(mission_id, report)
        return f"s3://{self._bucket}/{key}"

    def load_mission_report(self, mission_id: str) -> dict[str, object] | None:
        key = self._report_key(mission_id)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except Exception:
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


def _clean_env_value(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _normalize_mode(mode: str | None) -> str:
    if mode is None:
        return "local"
    normalized = mode.strip().lower()
    if normalized not in {"local", "s3"}:
        return "local"
    return normalized


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
