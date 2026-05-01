"""Unit tests for the SyncWorker drain loop (ADR-0007 §3)."""

from __future__ import annotations

from dataclasses import dataclass, field

from rescue_ai.domain.entities import (
    Alert,
    AutoDecision,
    Detection,
    FrameEvent,
    Mission,
    TrajectoryPoint,
)
from rescue_ai.domain.ports import OutboxRecord, OutboxRow
from rescue_ai.domain.value_objects import (
    AlertStatus,
    AutoDecisionKind,
    MissionMode,
    TrajectorySource,
)
from rescue_ai.infrastructure.sync.offline_first_repositories import (
    OfflineFirstAlertRepository,
    OfflineFirstAutoDecisionRepository,
    OfflineFirstFrameEventRepository,
    OfflineFirstMissionRepository,
    OfflineFirstTrajectoryRepository,
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


# ── Alert / FrameEvent / Trajectory / AutoDecision wrappers ───────


def _alert(alert_id: str = "a-1") -> Alert:
    primary = Detection(
        bbox=(0.0, 0.0, 10.0, 10.0),
        score=0.9,
        label="person",
        model_name="yolo",
    )
    return Alert(
        alert_id=alert_id,
        mission_id="m-1",
        frame_id=42,
        ts_sec=12.5,
        image_uri="s3://bucket/m-1/42.jpg",
        people_detected=1,
        primary_detection=primary,
        detections=[primary],
    )


class _StubAlertRepo:
    def __init__(self) -> None:
        self.added: list[Alert] = []
        self.review_results: dict[str, Alert] = {}

    def add(self, alert: Alert) -> None:
        self.added.append(alert)

    def get(self, alert_id: str) -> Alert | None:
        return next(
            (a for a in self.added if a.alert_id == alert_id), None
        )

    def list(
        self, mission_id: str | None = None, status: str | None = None
    ) -> list[Alert]:
        _ = (mission_id, status)
        return list(self.added)

    def update_status(self, alert_id, updates):
        if alert_id in self.review_results:
            return self.review_results[alert_id]
        existing = self.get(alert_id)
        if existing is None:
            return None
        existing.status = updates["status"]
        existing.reviewed_by = updates.get("reviewed_by")
        return existing


def test_offline_first_alert_repo_emits_outbox_on_add_and_review() -> None:
    inner = _StubAlertRepo()
    outbox = _InMemoryOutbox()
    repo = OfflineFirstAlertRepository(inner, outbox)

    alert = _alert("a-1")
    repo.add(alert)

    assert inner.added == [alert]
    assert len(outbox.rows) == 1
    assert outbox.rows[0].entity_type == "alert"
    assert outbox.rows[0].idempotency_key.startswith("alert:a-1:add:")
    assert outbox.rows[0].payload_json["alert_id"] == "a-1"

    repo.update_status(
        "a-1",
        updates={
            "status": AlertStatus.REVIEWED_CONFIRMED,
            "reviewed_by": "operator",
            "reviewed_at_sec": 100.0,
            "decision_reason": None,
        },
    )
    assert len(outbox.rows) == 2
    assert outbox.rows[1].idempotency_key == (
        f"alert:a-1:review:{AlertStatus.REVIEWED_CONFIRMED}:operator"
    )


def test_offline_first_alert_repo_no_outbox_when_review_returns_none() -> None:
    inner = _StubAlertRepo()
    outbox = _InMemoryOutbox()
    repo = OfflineFirstAlertRepository(inner, outbox)

    result = repo.update_status(
        "missing",
        updates={
            "status": AlertStatus.REVIEWED_CONFIRMED,
            "reviewed_by": "x",
            "reviewed_at_sec": 0.0,
            "decision_reason": None,
        },
    )
    assert result is None
    assert not outbox.rows


class _StubFrameEventRepo:
    def __init__(self) -> None:
        self.added: list[FrameEvent] = []

    def add(self, frame_event: FrameEvent) -> None:
        self.added.append(frame_event)

    def list_by_mission(self, mission_id: str) -> list[FrameEvent]:
        return [fe for fe in self.added if fe.mission_id == mission_id]


def test_offline_first_frame_event_repo_uses_natural_key() -> None:
    inner = _StubFrameEventRepo()
    outbox = _InMemoryOutbox()
    repo = OfflineFirstFrameEventRepository(inner, outbox)

    fe = FrameEvent(
        mission_id="m-1",
        frame_id=7,
        ts_sec=1.5,
        image_uri="s3://bucket/m-1/7.jpg",
        gt_person_present=False,
        gt_episode_id=None,
    )
    repo.add(fe)
    repo.add(fe)  # replay — same natural key, outbox dedupes

    assert inner.added == [fe, fe]
    assert len(outbox.rows) == 1  # dedup by idempotency_key
    assert outbox.rows[0].idempotency_key == "frame_event:m-1:7"


class _StubTrajectoryRepo:
    def __init__(self) -> None:
        self.added: list[TrajectoryPoint] = []

    def add(self, point: TrajectoryPoint) -> None:
        self.added.append(point)

    def list_by_mission(self, mission_id: str) -> list[TrajectoryPoint]:
        return [p for p in self.added if p.mission_id == mission_id]


def test_offline_first_trajectory_repo_uses_seq_in_key() -> None:
    inner = _StubTrajectoryRepo()
    outbox = _InMemoryOutbox()
    repo = OfflineFirstTrajectoryRepository(inner, outbox)

    point = TrajectoryPoint(
        mission_id="m-1",
        seq=3,
        ts_sec=0.5,
        x=1.0,
        y=2.0,
        z=0.5,
        source=TrajectorySource.MARKER,
        frame_id=18,
    )
    repo.add(point)

    assert inner.added == [point]
    assert outbox.rows[0].idempotency_key == "trajectory_point:m-1:3"
    assert outbox.rows[0].payload_json["seq"] == 3
    assert outbox.rows[0].payload_json["source"] == str(
        TrajectorySource.MARKER
    )


class _StubAutoDecisionRepo:
    def __init__(self) -> None:
        self.added: list[AutoDecision] = []

    def add(self, decision: AutoDecision) -> None:
        self.added.append(decision)

    def list_by_mission(self, mission_id: str) -> list[AutoDecision]:
        return [d for d in self.added if d.mission_id == mission_id]


def test_offline_first_auto_decision_repo_emits_insert_outbox_row() -> None:
    inner = _StubAutoDecisionRepo()
    outbox = _InMemoryOutbox()
    repo = OfflineFirstAutoDecisionRepository(inner, outbox)

    decision = AutoDecision(
        decision_id="d-1",
        mission_id="m-1",
        ts_sec=0.7,
        kind=AutoDecisionKind.ALERT_CREATED,
        reason="person detected",
        created_at="2026-04-28T00:00:00+00:00",
    )
    repo.add(decision)

    assert inner.added == [decision]
    assert outbox.rows[0].entity_type == "auto_decision"
    assert outbox.rows[0].operation == "insert"
    assert outbox.rows[0].idempotency_key == "auto_decision:d-1"
