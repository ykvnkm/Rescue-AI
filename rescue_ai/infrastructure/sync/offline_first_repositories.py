"""Offline-first repository wrappers.

Hybrid mode (ADR-0007 §3) writes to the **local** Postgres and, in the
same transaction, appends a `replication_outbox` row that the
`SyncWorker` later drains to the remote Postgres. The wrapper is
deliberately a thin decorator: it delegates the whole domain write to
the underlying repository and adds the outbox row, so business logic
stays unaware of the deployment profile.
"""

from __future__ import annotations

from typing import Any

from rescue_ai.domain.entities import Mission
from rescue_ai.domain.ports import (
    MissionRepository,
    OutboxRecord,
    SyncOutbox,
)


class OfflineFirstMissionRepository:
    """Mission repository that produces an outbox row on every mutation.

    The local Postgres is authoritative for reads. ``create`` /
    ``update_*`` go to the underlying repository first, then the
    outbox row is enqueued in a fresh transaction (the caller does not
    own the connection here, so we keep the boundary simple — re-runs
    are safe because ``idempotency_key`` is unique).
    """

    def __init__(
        self,
        inner: MissionRepository,
        outbox: SyncOutbox,
    ) -> None:
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


def _idempotency_key(*parts: str | None) -> str:
    return ":".join("" if part is None else str(part) for part in parts)


__all__ = ["OfflineFirstMissionRepository"]
