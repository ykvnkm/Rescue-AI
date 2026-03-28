"""Offline-first artifact storage: saves locally + enqueues S3 sync via outbox."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from rescue_ai.infrastructure.artifact_storage import LocalArtifactStorage
from rescue_ai.infrastructure.sync_outbox_repository import (
    PostgresSyncOutboxRepository,
)


class OfflineFirstArtifactStorage:
    """Stores artifacts locally and creates outbox entries for S3 upload."""

    def __init__(
        self,
        local_storage: LocalArtifactStorage,
        outbox: PostgresSyncOutboxRepository,
        s3_bucket: str,
        s3_prefix: str = "missions",
    ) -> None:
        self._local = local_storage
        self._outbox = outbox
        self._s3_bucket = s3_bucket
        self._s3_prefix = s3_prefix
        self._slug_cache: dict[str, str] = {}

    def register_slug(self, mission_id: str, slug: str) -> None:
        """Register a human-readable slug for a mission (called on create)."""
        self._slug_cache[mission_id] = slug

    def _s3_mission_prefix(self, mission_id: str) -> str:
        """Return S3 path prefix using slug if available, else mission_id."""
        slug = self._slug_cache.get(mission_id)
        if slug:
            return f"{self._s3_prefix}/{slug}"
        return f"{self._s3_prefix}/{mission_id}"

    def store_frame(self, mission_id: str, frame_id: int, source_uri: str) -> str:
        local_uri = self._local.store_frame(mission_id, frame_id, source_uri)

        suffix = Path(local_uri).suffix if local_uri != source_uri else ".bin"
        prefix = self._s3_mission_prefix(mission_id)
        s3_key = f"{prefix}/frames/{frame_id}{suffix}"
        idempotency_key = f"upload_s3:frame:{mission_id}:{frame_id}"

        self._outbox.enqueue(
            entity_type="frame",
            entity_id=f"{mission_id}:{frame_id}",
            operation="upload_s3",
            idempotency_key=idempotency_key,
            local_path=local_uri,
            s3_bucket=self._s3_bucket,
            s3_key=s3_key,
        )
        return local_uri

    def load_frame(self, image_uri: str):
        return self._local.load_frame(image_uri)

    def save_mission_report(
        self, mission_id: str, report: Mapping[str, object]
    ) -> str:
        local_uri = self._local.save_mission_report(mission_id, report)

        prefix = self._s3_mission_prefix(mission_id)
        s3_key = f"{prefix}/report.json"
        idempotency_key = f"upload_s3:report:{mission_id}"

        self._outbox.enqueue(
            entity_type="report",
            entity_id=mission_id,
            operation="upload_s3",
            idempotency_key=idempotency_key,
            local_path=local_uri,
            s3_bucket=self._s3_bucket,
            s3_key=s3_key,
        )
        return local_uri

    def load_mission_report(self, mission_id: str) -> Mapping[str, object] | None:
        return self._local.load_mission_report(mission_id)
