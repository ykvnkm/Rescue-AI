"""S3-compatible artifact storage adapter."""

from __future__ import annotations

import csv
import json
import mimetypes
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from urllib.parse import urlparse

from rescue_ai.config import StorageSettings
from rescue_ai.domain.value_objects import ArtifactBlob

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
    """Settings for S3 artifact backend."""

    endpoint: str | None = None
    region: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    bucket: str | None = None
    prefix: str | None = None

    @property
    def ready(self) -> bool:
        return bool(
            self.endpoint
            and self.region
            and self.access_key_id
            and self.secret_access_key
            and self.bucket
        )


def build_s3_storage(settings: StorageSettings) -> S3ArtifactStorage:
    """Build S3 artifact storage from settings. Raises if credentials missing."""
    if not settings.s3_access_key_id or not settings.s3_secret_access_key:
        raise RuntimeError(
            "S3 credentials required: set ARTIFACTS_S3_ACCESS_KEY_ID "
            "and ARTIFACTS_S3_SECRET_ACCESS_KEY"
        )
    if not settings.s3_bucket:
        raise RuntimeError("ARTIFACTS_S3_BUCKET is required")

    backend_settings = S3ArtifactBackendSettings(
        endpoint=settings.s3_endpoint,
        region=settings.s3_region,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
        bucket=settings.s3_bucket,
        prefix=settings.s3_prefix,
    )
    return S3ArtifactStorage(settings=backend_settings)


class S3ArtifactStorage:
    """Stores artifacts in S3-compatible bucket."""

    def __init__(self, settings: S3ArtifactBackendSettings) -> None:
        if boto3 is None:
            raise RuntimeError("boto3 is required for S3 storage")

        self._settings = settings
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

        _ = frame_id
        filename = source_path.name or "frame.bin"
        key = self._key_for_mission_file(
            mission_id=mission_id,
            leaf=f"frames/{filename}",
        )
        s3_uri = f"s3://{self._settings.bucket}/{key}"

        with self._lock:
            self._pending_frames[key] = PendingFrameUpload(source_uri=source_uri)

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
            return None
        bucket, key = parsed

        with self._lock:
            pending = self._pending_frames.get(key)
        if pending is not None and pending.error is not None:
            raise RuntimeError(pending.error)

        try:
            response = self._client.get_object(Bucket=bucket, Key=key)
        except S3_OPERATION_ERRORS as error:
            if _is_missing_s3_object_error(error):
                return None
            raise

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

    def save_mission_report(self, mission_id: str, report: Mapping[str, object]) -> str:
        key = self._report_key(mission_id)
        payload = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
        self._client.put_object(
            Bucket=self._settings.bucket,
            Key=key,
            Body=payload,
            ContentType="application/json",
        )
        return f"s3://{self._settings.bucket}/{key}"

    def save_mission_annotations(
        self,
        mission_id: str,
        payload: Mapping[str, object],
    ) -> str:
        key = self._annotations_key(mission_id)
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._client.put_object(
            Bucket=self._settings.bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )
        return f"s3://{self._settings.bucket}/{key}"

    def load_mission_report(self, mission_id: str) -> Mapping[str, object] | None:
        key = self._report_key(mission_id)
        try:
            response = self._client.get_object(Bucket=self._settings.bucket, Key=key)
        except S3_OPERATION_ERRORS as error:
            if _is_missing_s3_object_error(error):
                return None
            raise

        try:
            payload = json.loads(response["Body"].read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def write_report(self, run_key: str, payload: dict[str, object]) -> str:
        """Write a batch run report to S3."""
        safe_key = run_key.replace(":", "__")
        key = self._batch_report_key(safe_key)
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._client.put_object(Bucket=self._settings.bucket, Key=key, Body=body)
        return f"s3://{self._settings.bucket}/{key}"

    def write_debug_rows(self, run_key: str, rows: list[dict[str, object]]) -> str:
        """Write batch debug rows as CSV to S3."""
        safe_key = run_key.replace(":", "__")
        key = self._batch_debug_key(safe_key)
        headers = sorted({item for row in rows for item in row.keys()}) if rows else []
        buffer = StringIO()
        writer = csv.DictWriter(buffer, fieldnames=headers)
        if headers:
            writer.writeheader()
            writer.writerows(rows)
        self._client.put_object(
            Bucket=self._settings.bucket,
            Key=key,
            Body=buffer.getvalue().encode("utf-8"),
        )
        return f"s3://{self._settings.bucket}/{key}"

    def _report_key(self, mission_id: str) -> str:
        return self._key_for_mission_file(mission_id=mission_id, leaf="report.json")

    def _annotations_key(self, mission_id: str) -> str:
        return self._key_for_mission_file(
            mission_id=mission_id,
            leaf="annotations/mission.json",
        )

    def _batch_report_key(self, safe_run_key: str) -> str:
        return self._join(
            self._settings.prefix or "",
            "batch",
            "runs",
            safe_run_key,
            "report.json",
        )

    def _batch_debug_key(self, safe_run_key: str) -> str:
        return self._join(
            self._settings.prefix or "",
            "batch",
            "runs",
            safe_run_key,
            "debug.csv",
        )

    def _key_for_mission_file(self, *, mission_id: str, leaf: str) -> str:
        return self._join(self._settings.prefix or "", mission_id, leaf)

    @staticmethod
    def _join(*parts: str) -> str:
        return "/".join(part.strip("/") for part in parts if part.strip("/"))

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
    """Pending async frame upload tracking."""

    source_uri: str
    error: str | None = None
