"""Offline-first repository decorators that enqueue outbox entries for remote sync."""

from __future__ import annotations

import json

from rescue_ai.domain.entities import Alert, Detection, FrameEvent, Mission
from rescue_ai.domain.ports import AlertReviewPayload
from rescue_ai.infrastructure.postgres_connection import PostgresDatabase
from rescue_ai.infrastructure.postgres_repositories import (
    PostgresAlertRepository,
    PostgresFrameEventRepository,
    PostgresMissionRepository,
)
from rescue_ai.infrastructure.sync_outbox_repository import (
    PostgresSyncOutboxRepository,
)


def _serialize_mission(mission: Mission) -> dict[str, object]:
    return {
        "mission_id": mission.mission_id,
        "source_name": mission.source_name,
        "status": mission.status,
        "created_at": mission.created_at,
        "total_frames": mission.total_frames,
        "fps": mission.fps,
        "completed_frame_id": mission.completed_frame_id,
        "slug": mission.slug,
    }


def _serialize_frame_event(fe: FrameEvent) -> dict[str, object]:
    return {
        "mission_id": fe.mission_id,
        "frame_id": fe.frame_id,
        "ts_sec": fe.ts_sec,
        "image_uri": fe.image_uri,
        "gt_person_present": fe.gt_person_present,
        "gt_episode_id": fe.gt_episode_id,
    }


def _serialize_detection(d: Detection) -> dict[str, object]:
    return {
        "bbox": list(d.bbox),
        "score": d.score,
        "label": d.label,
        "model_name": d.model_name,
        "explanation": d.explanation,
    }


def _serialize_alert(alert: Alert) -> dict[str, object]:
    return {
        "alert_id": alert.alert_id,
        "mission_id": alert.mission_id,
        "frame_id": alert.frame_id,
        "ts_sec": alert.ts_sec,
        "image_uri": alert.image_uri,
        "people_detected": alert.people_detected,
        "primary_bbox": list(alert.primary_detection.bbox),
        "primary_score": alert.primary_detection.score,
        "primary_label": alert.primary_detection.label,
        "primary_model_name": alert.primary_detection.model_name,
        "primary_explanation": alert.primary_detection.explanation,
        "detections": [_serialize_detection(d) for d in alert.detections],
        "status": alert.status.value if hasattr(alert.status, "value") else str(alert.status),
        "reviewed_by": alert.reviewed_by,
        "reviewed_at_sec": alert.reviewed_at_sec,
        "decision_reason": alert.decision_reason,
    }


class OfflineFirstMissionRepository:
    """Wraps PostgresMissionRepository and enqueues remote PG sync via outbox."""

    def __init__(
        self,
        inner: PostgresMissionRepository,
        outbox: PostgresSyncOutboxRepository,
    ) -> None:
        self._inner = inner
        self._outbox = outbox

    def create(self, mission: Mission) -> None:
        self._inner.create(mission)
        self._outbox.enqueue(
            entity_type="mission",
            entity_id=mission.mission_id,
            operation="upsert_remote_pg",
            idempotency_key=f"upsert_pg:mission:create:{mission.mission_id}",
            payload_json=_serialize_mission(mission),
        )

    def get(self, mission_id: str) -> Mission | None:
        return self._inner.get(mission_id)

    def update_details(
        self,
        mission_id: str,
        *,
        source_name: str | None = None,
        total_frames: int | None = None,
        fps: float | None = None,
    ) -> Mission | None:
        result = self._inner.update_details(
            mission_id,
            source_name=source_name,
            total_frames=total_frames,
            fps=fps,
        )
        if result is not None:
            self._outbox.enqueue(
                entity_type="mission",
                entity_id=mission_id,
                operation="upsert_remote_pg",
                idempotency_key=f"upsert_pg:mission:update:{mission_id}:{result.status}",
                payload_json=_serialize_mission(result),
            )
        return result

    def update_status(
        self,
        mission_id: str,
        status: str,
        completed_frame_id: int | None = None,
    ) -> Mission | None:
        result = self._inner.update_status(mission_id, status, completed_frame_id)
        if result is not None:
            self._outbox.enqueue(
                entity_type="mission",
                entity_id=mission_id,
                operation="upsert_remote_pg",
                idempotency_key=f"upsert_pg:mission:status:{mission_id}:{status}",
                payload_json=_serialize_mission(result),
            )
        return result


class OfflineFirstAlertRepository:
    """Wraps PostgresAlertRepository and enqueues remote PG sync via outbox."""

    def __init__(
        self,
        inner: PostgresAlertRepository,
        outbox: PostgresSyncOutboxRepository,
    ) -> None:
        self._inner = inner
        self._outbox = outbox

    def add(self, alert: Alert) -> None:
        self._inner.add(alert)
        self._outbox.enqueue(
            entity_type="alert",
            entity_id=alert.alert_id,
            operation="upsert_remote_pg",
            idempotency_key=f"upsert_pg:alert:{alert.alert_id}",
            payload_json=_serialize_alert(alert),
        )

    def get(self, alert_id: str) -> Alert | None:
        return self._inner.get(alert_id)

    def list(
        self,
        mission_id: str | None = None,
        status: str | None = None,
    ) -> list[Alert]:
        return self._inner.list(mission_id=mission_id, status=status)

    def update_status(
        self,
        alert_id: str,
        updates: AlertReviewPayload,
    ) -> Alert | None:
        result = self._inner.update_status(alert_id, updates)
        if result is not None:
            self._outbox.enqueue(
                entity_type="alert",
                entity_id=alert_id,
                operation="upsert_remote_pg",
                idempotency_key=f"upsert_pg:alert:review:{alert_id}",
                payload_json=_serialize_alert(result),
            )
        return result


class OfflineFirstFrameEventRepository:
    """Wraps PostgresFrameEventRepository and enqueues remote PG sync via outbox."""

    def __init__(
        self,
        inner: PostgresFrameEventRepository,
        outbox: PostgresSyncOutboxRepository,
    ) -> None:
        self._inner = inner
        self._outbox = outbox

    def add(self, frame_event: FrameEvent) -> None:
        self._inner.add(frame_event)
        self._outbox.enqueue(
            entity_type="frame_event",
            entity_id=f"{frame_event.mission_id}:{frame_event.frame_id}",
            operation="upsert_remote_pg",
            idempotency_key=f"upsert_pg:frame_event:{frame_event.mission_id}:{frame_event.frame_id}",
            payload_json=_serialize_frame_event(frame_event),
        )

    def list_by_mission(self, mission_id: str) -> list[FrameEvent]:
        return self._inner.list_by_mission(mission_id)
