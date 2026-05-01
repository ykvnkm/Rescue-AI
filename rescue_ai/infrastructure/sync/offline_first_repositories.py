"""Offline-first repository wrappers (ADR-0007 §3).

Hybrid mode (`DEPLOYMENT_MODE=hybrid`) writes to the **local** Postgres
and, atomically with that write, appends a row to ``replication_outbox``
that the :class:`SyncWorker` later drains to the remote Postgres.

The wrappers in this module are **thin decorators** around the existing
postgres repositories: they delegate every domain write to the inner
repo and only add the outbox bookkeeping. Business logic stays unaware
of which deployment profile is active, so the rest of the codebase
keeps depending on the same domain ports (`MissionRepository`,
`AlertRepository`, `FrameEventRepository`, …).

Coverage of the outbox:

  - :class:`OfflineFirstMissionRepository`        — Mission lifecycle
  - :class:`OfflineFirstAlertRepository`          — Alert add / review
  - :class:`OfflineFirstFrameEventRepository`     — frame stream
  - :class:`OfflineFirstTrajectoryRepository`     — trajectory points
  - :class:`OfflineFirstAutoDecisionRepository`   — auto-mode decisions

Idempotency keys are deterministic per logical event so a retry never
produces a duplicate downstream (see remote target ``ON CONFLICT
(idempotency_key) DO UPDATE``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rescue_ai.domain.entities import (
    Alert,
    AutoDecision,
    FrameEvent,
    Mission,
    TrajectoryPoint,
)
from rescue_ai.domain.ports import (
    AlertRepository,
    AlertReviewPayload,
    AutoDecisionRepository,
    AutoMissionConfigRepository,
    FrameEventRepository,
    MissionRepository,
    OutboxRecord,
    SyncOutbox,
    TrajectoryRepository,
)
from rescue_ai.domain.value_objects import NavMode


# ── Mission ────────────────────────────────────────────────────────


class OfflineFirstMissionRepository:
    """Mission repository that produces an outbox row on every mutation."""

    def __init__(self, inner: MissionRepository, outbox: SyncOutbox) -> None:
        self._inner = inner
        self._outbox = outbox

    def create(self, mission: Mission) -> None:
        self._inner.create(mission)
        self._outbox.enqueue(
            OutboxRecord(
                entity_type="mission",
                entity_id=mission.mission_id,
                operation="upsert",
                payload_json=_mission_payload(mission),
                idempotency_key=_idempotency_key(
                    "mission",
                    mission.mission_id,
                    "create",
                    mission.status,
                ),
            )
        )

    def get(self, mission_id: str) -> Mission | None:
        return self._inner.get(mission_id)

    def list(self, status: str | None = None) -> list[Mission]:
        return self._inner.list(status=status)

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
                OutboxRecord(
                    entity_type="mission",
                    entity_id=mission_id,
                    operation="upsert",
                    payload_json=_mission_payload(result),
                    idempotency_key=_idempotency_key(
                        "mission",
                        mission_id,
                        "details",
                        str(result.total_frames),
                        f"{result.fps}",
                    ),
                )
            )
        return result

    def update_status(
        self,
        mission_id: str,
        status: str,
        completed_frame_id: int | None = None,
    ) -> Mission | None:
        result = self._inner.update_status(
            mission_id,
            status=status,
            completed_frame_id=completed_frame_id,
        )
        if result is not None:
            self._outbox.enqueue(
                OutboxRecord(
                    entity_type="mission",
                    entity_id=mission_id,
                    operation="upsert",
                    payload_json=_mission_payload(result),
                    idempotency_key=_idempotency_key(
                        "mission",
                        mission_id,
                        "status",
                        status,
                        str(completed_frame_id),
                    ),
                )
            )
        return result


# ── Alert ──────────────────────────────────────────────────────────


class OfflineFirstAlertRepository:
    """Alert repository emitting outbox rows on add / review."""

    def __init__(self, inner: AlertRepository, outbox: SyncOutbox) -> None:
        self._inner = inner
        self._outbox = outbox

    def add(self, alert: Alert) -> None:
        self._inner.add(alert)
        self._outbox.enqueue(
            OutboxRecord(
                entity_type="alert",
                entity_id=alert.alert_id,
                operation="upsert",
                payload_json=_alert_payload(alert),
                idempotency_key=_idempotency_key(
                    "alert",
                    alert.alert_id,
                    "add",
                    str(alert.status),
                ),
            )
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
                OutboxRecord(
                    entity_type="alert",
                    entity_id=alert_id,
                    operation="upsert",
                    payload_json=_alert_payload(result),
                    idempotency_key=_idempotency_key(
                        "alert",
                        alert_id,
                        "review",
                        str(result.status),
                        result.reviewed_by or "",
                    ),
                )
            )
        return result


# ── FrameEvent ─────────────────────────────────────────────────────


class OfflineFirstFrameEventRepository:
    """FrameEvent repository emitting outbox rows on add."""

    def __init__(
        self, inner: FrameEventRepository, outbox: SyncOutbox
    ) -> None:
        self._inner = inner
        self._outbox = outbox

    def add(self, frame_event: FrameEvent) -> None:
        self._inner.add(frame_event)
        # FrameEvent natural key is (mission_id, frame_id) — both are
        # required and stable, so the idempotency_key is collision-free.
        self._outbox.enqueue(
            OutboxRecord(
                entity_type="frame_event",
                entity_id=f"{frame_event.mission_id}:{frame_event.frame_id}",
                operation="upsert",
                payload_json=_frame_event_payload(frame_event),
                idempotency_key=_idempotency_key(
                    "frame_event",
                    frame_event.mission_id,
                    str(frame_event.frame_id),
                ),
            )
        )

    def list_by_mission(self, mission_id: str) -> list[FrameEvent]:
        return self._inner.list_by_mission(mission_id)


# ── TrajectoryPoint ────────────────────────────────────────────────


class OfflineFirstTrajectoryRepository:
    """TrajectoryRepository wrapper for hybrid replication."""

    def __init__(
        self, inner: TrajectoryRepository, outbox: SyncOutbox
    ) -> None:
        self._inner = inner
        self._outbox = outbox

    def add(self, point: TrajectoryPoint) -> None:
        self._inner.add(point)
        self._outbox.enqueue(
            OutboxRecord(
                entity_type="trajectory_point",
                entity_id=f"{point.mission_id}:{point.seq}",
                operation="upsert",
                payload_json=_trajectory_payload(point),
                idempotency_key=_idempotency_key(
                    "trajectory_point",
                    point.mission_id,
                    str(point.seq),
                ),
            )
        )

    def list_by_mission(self, mission_id: str) -> list[TrajectoryPoint]:
        return self._inner.list_by_mission(mission_id)


# ── AutoDecision ───────────────────────────────────────────────────


class OfflineFirstAutoDecisionRepository:
    """AutoDecisionRepository wrapper for hybrid replication."""

    def __init__(
        self, inner: AutoDecisionRepository, outbox: SyncOutbox
    ) -> None:
        self._inner = inner
        self._outbox = outbox

    def add(self, decision: AutoDecision) -> None:
        self._inner.add(decision)
        self._outbox.enqueue(
            OutboxRecord(
                entity_type="auto_decision",
                entity_id=decision.decision_id,
                operation="insert",
                payload_json=_auto_decision_payload(decision),
                idempotency_key=_idempotency_key(
                    "auto_decision",
                    decision.decision_id,
                ),
            )
        )

    def list_by_mission(self, mission_id: str) -> list[AutoDecision]:
        return self._inner.list_by_mission(mission_id)


# ── AutoMissionConfig (per-mission snapshot) ──────────────────────


class OfflineFirstAutoMissionConfigRepository:
    """One outbox row per mission start; replays are idempotent."""

    def __init__(
        self, inner: AutoMissionConfigRepository, outbox: SyncOutbox
    ) -> None:
        self._inner = inner
        self._outbox = outbox

    def save(
        self,
        *,
        mission_id: str,
        nav_mode: NavMode,
        detector: str,
        config_json: Mapping[str, object],
    ) -> None:
        self._inner.save(
            mission_id=mission_id,
            nav_mode=nav_mode,
            detector=detector,
            config_json=config_json,
        )
        self._outbox.enqueue(
            OutboxRecord(
                entity_type="auto_mission_config",
                entity_id=mission_id,
                operation="upsert",
                payload_json={
                    "mission_id": mission_id,
                    "nav_mode": str(nav_mode),
                    "detector": detector,
                    "config_json": dict(config_json),
                },
                idempotency_key=_idempotency_key(
                    "auto_mission_config",
                    mission_id,
                ),
            )
        )

    def get(self, mission_id: str) -> Mapping[str, object] | None:
        return self._inner.get(mission_id)


# ── payload encoders ──────────────────────────────────────────────


def _mission_payload(mission: Mission) -> dict[str, Any]:
    return {
        "mission_id": mission.mission_id,
        "source_name": mission.source_name,
        "status": mission.status,
        "created_at": mission.created_at,
        "total_frames": mission.total_frames,
        "fps": mission.fps,
        "completed_frame_id": mission.completed_frame_id,
        "slug": mission.slug,
        "mode": str(mission.mode),
    }


def _alert_payload(alert: Alert) -> dict[str, Any]:
    primary = alert.primary_detection
    return {
        "alert_id": alert.alert_id,
        "mission_id": alert.mission_id,
        "frame_id": alert.frame_id,
        "ts_sec": alert.ts_sec,
        "image_uri": alert.image_uri,
        "people_detected": alert.people_detected,
        "status": str(alert.status),
        "reviewed_by": alert.reviewed_by,
        "reviewed_at_sec": alert.reviewed_at_sec,
        "decision_reason": alert.decision_reason,
        "primary_detection": {
            "bbox": list(primary.bbox),
            "score": primary.score,
            "label": primary.label,
            "model_name": primary.model_name,
            "explanation": primary.explanation,
        },
        "detections": [
            {
                "bbox": list(det.bbox),
                "score": det.score,
                "label": det.label,
                "model_name": det.model_name,
                "explanation": det.explanation,
            }
            for det in alert.detections
        ],
    }


def _frame_event_payload(frame_event: FrameEvent) -> dict[str, Any]:
    return {
        "mission_id": frame_event.mission_id,
        "frame_id": frame_event.frame_id,
        "ts_sec": frame_event.ts_sec,
        "image_uri": frame_event.image_uri,
        "gt_person_present": frame_event.gt_person_present,
        "gt_episode_id": frame_event.gt_episode_id,
    }


def _trajectory_payload(point: TrajectoryPoint) -> dict[str, Any]:
    return {
        "mission_id": point.mission_id,
        "seq": point.seq,
        "ts_sec": point.ts_sec,
        "x": point.x,
        "y": point.y,
        "z": point.z,
        "source": str(point.source),
        "frame_id": point.frame_id,
    }


def _auto_decision_payload(decision: AutoDecision) -> dict[str, Any]:
    return {
        "decision_id": decision.decision_id,
        "mission_id": decision.mission_id,
        "ts_sec": decision.ts_sec,
        "kind": str(decision.kind),
        "reason": decision.reason,
        "created_at": decision.created_at,
        "frame_id": decision.frame_id,
    }


def _idempotency_key(*parts: str | None) -> str:
    return ":".join("" if part is None else str(part) for part in parts)


__all__ = [
    "OfflineFirstMissionRepository",
    "OfflineFirstAlertRepository",
    "OfflineFirstFrameEventRepository",
    "OfflineFirstTrajectoryRepository",
    "OfflineFirstAutoDecisionRepository",
    "OfflineFirstAutoMissionConfigRepository",
]
