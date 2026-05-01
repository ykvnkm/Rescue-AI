"""Unit tests for the SyncWorker drain loop (ADR-0007 §3)."""

from __future__ import annotations

from dataclasses import dataclass, field

from rescue_ai.domain.entities import Mission
from rescue_ai.domain.ports import OutboxRecord, OutboxRow
from rescue_ai.domain.value_objects import MissionMode
from rescue_ai.infrastructure.sync.offline_first_repositories import (
    OfflineFirstMissionRepository,
)
from rescue_ai.infrastructure.sync.sync_worker import SyncWorker, SyncWorkerConfig

# ── In-memory fakes ─────────────────────────────────────────────────


@dataclass
class _InMemoryOutbox:
    rows: list[OutboxRow] = field(default_factory=list)
    synced: list[int] = field(default_factory=list)
    failures: list[tuple[int, str]] = field(default_factory=list)
    stuck_resets: list[float] = field(default_factory=list)
    _seen_keys: set[str] = field(default_factory=set)
    _next_id: int = 1

    def enqueue(self, record: OutboxRecord, *, conn: object | None = None) -> None:
        _ = conn
        if record.idempotency_key in self._seen_keys:
            return  # mirror ON CONFLICT DO NOTHING
        self._seen_keys.add(record.idempotency_key)
        self.rows.append(
            OutboxRow(
                id=self._next_id,
                entity_type=record.entity_type,
                entity_id=record.entity_id,
                operation=record.operation,
                payload_json=dict(record.payload_json),
                local_path=record.local_path,
                s3_bucket=record.s3_bucket,
                s3_key=record.s3_key,
                idempotency_key=record.idempotency_key,
                attempts=0,
            )
        )
        self._next_id += 1

    def claim_pending(self, batch_size: int) -> list[OutboxRow]:
        claimed = self.rows[:batch_size]
        self.rows = self.rows[batch_size:]
        return claimed

    def mark_synced(self, outbox_id: int) -> None:
        self.synced.append(outbox_id)

    def mark_failed(self, outbox_id: int, error: str) -> None:
        self.failures.append((outbox_id, error))
        # mimic the postgres adapter: row goes back to the queue with
        # attempts incremented.
        self.rows.append(
            OutboxRow(
                id=outbox_id,
                entity_type="mission",
                entity_id="m-1",
                operation="upsert",
                payload_json={},
                local_path=None,
                s3_bucket=None,
                s3_key=None,
                idempotency_key=f"retry-{outbox_id}-{len(self.failures)}",
                attempts=len([row for row in self.failures if row[0] == outbox_id]),
            )
        )

    def reset_stuck(self, processing_timeout_sec: float) -> int:
        self.stuck_resets.append(processing_timeout_sec)
        return 0


class _RecordingTarget:
    def __init__(self) -> None:
        self.delivered: list[OutboxRow] = []

    def deliver(self, row: OutboxRow) -> None:
        self.delivered.append(row)


class _IdempotentTarget:
    """Real-shaped target: tolerates duplicates by idempotency_key."""

    def __init__(self) -> None:
        self.applied: dict[str, OutboxRow] = {}

    def deliver(self, row: OutboxRow) -> None:
        self.applied[row.idempotency_key] = row


class _FailingThenOkTarget:
    def __init__(self, fail_times: int) -> None:
        self._fail_times = fail_times
        self.calls = 0
        self.delivered: list[OutboxRow] = []

    def deliver(self, row: OutboxRow) -> None:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError(f"simulated network failure {self.calls}")
        self.delivered.append(row)


# ── Tests ───────────────────────────────────────────────────────────


def _mission(mission_id: str = "m-1") -> Mission:
    return Mission(
        mission_id=mission_id,
        source_name="rpi",
        status="started",
        created_at="2026-04-27T12:00:00+00:00",
        total_frames=0,
        fps=10.0,
        mode=MissionMode.OPERATOR,
    )


def test_sync_worker_drains_one_batch_and_marks_synced() -> None:
    outbox = _InMemoryOutbox()
    target = _RecordingTarget()
    outbox.enqueue(
        OutboxRecord(
            entity_type="mission",
            entity_id="m-1",
            operation="upsert",
            payload_json={"mission_id": "m-1"},
            idempotency_key="mission:m-1:create:started",
        )
    )

    worker = SyncWorker(outbox, target, SyncWorkerConfig(batch_size=10))
    synced = worker.run_once()

    assert synced == 1
    assert outbox.synced == [1]
    assert len(target.delivered) == 1
    assert outbox.stuck_resets == [SyncWorkerConfig().processing_timeout_sec]


def test_sync_worker_recovers_after_transient_failure() -> None:
    outbox = _InMemoryOutbox()
    target = _FailingThenOkTarget(fail_times=1)
    outbox.enqueue(
        OutboxRecord(
            entity_type="mission",
            entity_id="m-1",
            operation="upsert",
            payload_json={"mission_id": "m-1"},
            idempotency_key="mission:m-1:create:started",
        )
    )

    worker = SyncWorker(outbox, target, SyncWorkerConfig(batch_size=10))
    # First pass: target raises, row goes back to pending.
    assert worker.run_once() == 0
    assert outbox.failures and "simulated" in outbox.failures[0][1]

    # Second pass: target accepts, row marked synced.
    assert worker.run_once() == 1
    assert len(target.delivered) == 1


def test_sync_worker_does_not_double_apply_on_replay() -> None:
    """At-least-once + idempotency_key ⇒ no duplicates downstream."""
    outbox = _InMemoryOutbox()
    target = _IdempotentTarget()

    # Simulate the offline-first wrapper enqueuing the same logical
    # mission twice (e.g. retry after a crash before mark_synced).
    record = OutboxRecord(
        entity_type="mission",
        entity_id="m-1",
        operation="upsert",
        payload_json={"mission_id": "m-1", "status": "started"},
        idempotency_key="mission:m-1:create:started",
    )
    outbox.enqueue(record)
    outbox.enqueue(record)  # ON CONFLICT DO NOTHING

    worker = SyncWorker(outbox, target, SyncWorkerConfig(batch_size=10))
    worker.run_once()

    assert len(target.applied) == 1
    assert "mission:m-1:create:started" in target.applied


def test_sync_worker_stops_retrying_after_max_attempts() -> None:
    outbox = _InMemoryOutbox()
    target = _RecordingTarget()
    # Pre-seed a row with attempts already at the cap.
    outbox.rows.append(
        OutboxRow(
            id=42,
            entity_type="mission",
            entity_id="m-1",
            operation="upsert",
            payload_json={},
            local_path=None,
            s3_bucket=None,
            s3_key=None,
            idempotency_key="mission:m-1:capped",
            attempts=10,
        )
    )

    worker = SyncWorker(outbox, target, SyncWorkerConfig(max_attempts=10))
    worker.run_once()

    assert not target.delivered
    assert outbox.failures and outbox.failures[0][0] == 42


# ── OfflineFirstMissionRepository ─────────────────────────────────


class _StubMissionRepo:
    """Minimal mission repository stub for offline-first adapter tests."""

    def __init__(self) -> None:
        self.created: list[Mission] = []
        self.status_updates: list[tuple[str, str, int | None]] = []

    def create(self, mission: Mission) -> None:
        self.created.append(mission)

    def get(self, mission_id: str) -> Mission | None:
        return next((m for m in self.created if m.mission_id == mission_id), None)

    def list(self, status: str | None = None) -> list[Mission]:
        _ = status
        return list(self.created)

    def update_details(
        self, mission_id, *, source_name=None, total_frames=None, fps=None
    ):
        _ = (source_name, total_frames, fps)
        return self.get(mission_id)

    def update_status(self, mission_id, status, completed_frame_id=None):
        self.status_updates.append((mission_id, status, completed_frame_id))
        existing = self.get(mission_id)
        if existing is None:
            return None
        existing.status = status
        existing.completed_frame_id = completed_frame_id
        return existing


def test_offline_first_repo_writes_local_then_outbox() -> None:
    inner = _StubMissionRepo()
    outbox = _InMemoryOutbox()
    repo = OfflineFirstMissionRepository(inner=inner, outbox=outbox)

    mission = _mission("m-1")
    repo.create(mission)

    assert inner.created == [mission]
    assert len(outbox.rows) == 1
    row = outbox.rows[0]
    assert row.entity_type == "mission"
    assert row.entity_id == "m-1"
    assert row.idempotency_key == "mission:m-1:create:started"
    assert row.payload_json["mission_id"] == "m-1"


def test_offline_first_repo_status_update_emits_outbox_row() -> None:
    inner = _StubMissionRepo()
    outbox = _InMemoryOutbox()
    repo = OfflineFirstMissionRepository(inner=inner, outbox=outbox)
    repo.create(_mission("m-1"))
    outbox.rows.clear()  # ignore the create row for this assertion
    outbox._seen_keys.clear()

    repo.update_status("m-1", status="completed", completed_frame_id=99)

    assert len(outbox.rows) == 1
    row = outbox.rows[0]
    assert row.idempotency_key == "mission:m-1:status:completed:99"
    assert row.payload_json["status"] == "completed"


def test_offline_first_repo_skips_outbox_when_inner_returns_none() -> None:
    inner = _StubMissionRepo()
    outbox = _InMemoryOutbox()
    repo = OfflineFirstMissionRepository(inner=inner, outbox=outbox)

    result = repo.update_status("missing", status="completed")

    assert result is None
    assert not outbox.rows
