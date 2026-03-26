from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from rescue_ai.application.batch_runner import (
    BatchRunRequest,
    FrameRecord,
    MissionBatchRunner,
    MissionBatchRunnerDeps,
    MissionEngineFactoryPort,
    MissionEnginePort,
    MissionInput,
    RunStatusRecord,
)
from rescue_ai.domain.entities import Alert, AlertRuleConfig, Detection, FrameEvent
from rescue_ai.infrastructure.s3_artifact_store import LocalArtifactStorage
from rescue_ai.infrastructure.status_store import JsonStatusStore


class FakeSource:
    """Mission source test double with fixed payload."""

    def __init__(self, mission_input: MissionInput) -> None:
        self._mission_input = mission_input

    def load(self, mission_id: str, ds: str) -> MissionInput:
        _ = (mission_id, ds)
        return self._mission_input

    def describe_source(self) -> str:
        return "fake-source"


class FakeDetector:
    """Detector test double that always returns one person detection."""

    def detect(self, image_uri: str) -> list[Detection]:
        _ = image_uri
        return [
            Detection(
                bbox=(0.0, 0.0, 1.0, 1.0),
                score=0.95,
                label="person",
                model_name="yolo-model",
            )
        ]

    def warmup(self) -> None:
        return

    def runtime_name(self) -> str:
        return "fake-detector"


class ErrorDetector:
    """Detector test double that fails for every frame."""

    def detect(self, image_uri: str) -> list[Detection]:
        _ = image_uri
        raise RuntimeError("detector boom")

    def warmup(self) -> None:
        return

    def runtime_name(self) -> str:
        return "error-detector"


@dataclass
class FakeEngine(MissionEnginePort):
    """Mission engine test double with deterministic report semantics."""

    reviewed: list[str]
    alerts_total: int = 0

    def create_and_start_mission(
        self,
        source_name: str,
        total_frames: int,
        fps: float,
        report_metadata: dict[str, object],
    ) -> str:
        _ = (source_name, total_frames, fps, report_metadata)
        return "internal-mission-1"

    def ingest_frame(
        self,
        mission_id: str,
        frame_event: FrameEvent,
        detections: list[Detection],
    ) -> list[Alert]:
        if not detections:
            return []
        self.alerts_total += 1
        return [
            Alert(
                alert_id=f"alert-{frame_event.frame_id}",
                mission_id=mission_id,
                frame_id=frame_event.frame_id,
                ts_sec=frame_event.ts_sec,
                image_uri=frame_event.image_uri,
                people_detected=1,
                primary_detection=Detection(
                    bbox=(0.0, 0.0, 1.0, 1.0),
                    score=0.95,
                    label="person",
                    model_name="yolo-model",
                ),
            )
        ]

    def review_alert(
        self,
        alert_id: str,
        status: str,
        reviewed_at_sec: float,
        reason: str,
    ) -> None:
        _ = (status, reviewed_at_sec, reason)
        self.reviewed.append(alert_id)

    def complete(self, mission_id: str, completed_frame_id: int | None) -> None:
        _ = (mission_id, completed_frame_id)

    def build_report(self, mission_id: str) -> dict[str, object]:
        _ = mission_id
        return {
            "alerts_total": self.alerts_total,
            "alerts_confirmed": self.alerts_total,
            "alerts_rejected": 0,
            "false_alerts_total": 0,
            "recall_event": 1.0,
            "episodes_total": 1,
            "episodes_found": 1,
            "ttfc_sec": 0.3,
        }


class FakeEngineFactory(MissionEngineFactoryPort):
    """Factory that always returns the same fake mission engine."""

    def __init__(self, engine: FakeEngine) -> None:
        self.engine = engine

    def create(
        self,
        alert_rules: AlertRuleConfig,
        report_metadata: dict[str, object],
    ) -> MissionEnginePort:
        _ = (alert_rules, report_metadata)
        return self.engine

    def factory_name(self) -> str:
        return "fake-engine-factory"


def _runner(
    *,
    mission_input: MissionInput,
    detector: FakeDetector | ErrorDetector,
    statuses: JsonStatusStore,
    temp_dir: str,
    engine: FakeEngine,
) -> MissionBatchRunner:
    return MissionBatchRunner(
        MissionBatchRunnerDeps(
            source=FakeSource(mission_input),
            detector=detector,
            artifacts=LocalArtifactStorage(root=Path(temp_dir) / "artifacts"),
            statuses=statuses,
            engine_factory=FakeEngineFactory(engine),
        )
    )


def _request(force: bool = False) -> BatchRunRequest:
    return BatchRunRequest(
        mission_id="mission-1",
        ds="2026-03-01",
        config_hash="cfg",
        model_version="model-v1",
        code_version="code-v1",
        alert_rules=AlertRuleConfig(),
        force=force,
    )


def _frame(
    frame_id: int, gt_person_present: bool, is_corrupted: bool = False
) -> FrameRecord:
    return FrameRecord(
        frame_id=frame_id,
        ts_sec=float(frame_id),
        frame_path=Path(f"/tmp/frame_{frame_id}.jpg"),
        image_uri=f"/tmp/frame_{frame_id}.jpg",
        gt_person_present=gt_person_present,
        is_corrupted=is_corrupted,
    )


def test_batch_skip_when_completed_and_not_force() -> None:
    with TemporaryDirectory() as temp_dir:
        status_store = JsonStatusStore(path=Path(temp_dir) / "runs.json")
        status_store.upsert(
            RunStatusRecord(
                run_key=_request().run_key,
                status="completed",
                report_uri="r",
                debug_uri="d",
            )
        )
        runner = _runner(
            mission_input=MissionInput(
                source_uri="s", frames=[_frame(1, True)], gt_available=True
            ),
            detector=FakeDetector(),
            statuses=status_store,
            temp_dir=temp_dir,
            engine=FakeEngine(reviewed=[]),
        )
        result = runner.run(_request())

    assert result.status == "completed"
    assert result.report == {"idempotent_skip": True}


def test_batch_no_gt_does_not_auto_review_and_marks_kpi_not_applicable() -> None:
    with TemporaryDirectory() as temp_dir:
        engine = FakeEngine(reviewed=[])
        runner = _runner(
            mission_input=MissionInput(
                source_uri="s", frames=[_frame(1, False)], gt_available=False
            ),
            detector=FakeDetector(),
            statuses=JsonStatusStore(path=Path(temp_dir) / "runs.json"),
            temp_dir=temp_dir,
            engine=engine,
        )

        result = runner.run(_request())

    assert not engine.reviewed
    assert result.report["recall_event"] is None
    assert result.report["episodes_total"] is None
    assert result.report["precision_alert"] is None
    kpi_validity = cast(dict[str, str], result.report["kpi_validity"])
    assert kpi_validity["recall_event"] == "not_applicable"


def test_batch_partial_status_on_corrupted_rate() -> None:
    with TemporaryDirectory() as temp_dir:
        status_store = JsonStatusStore(path=Path(temp_dir) / "runs.json")
        frames = [_frame(1, True), _frame(2, True, is_corrupted=True)]
        runner = _runner(
            mission_input=MissionInput(
                source_uri="s", frames=frames, gt_available=True
            ),
            detector=FakeDetector(),
            statuses=status_store,
            temp_dir=temp_dir,
            engine=FakeEngine(reviewed=[]),
        )

        result = runner.run(_request(force=True))
        record = status_store.get(_request(force=True).run_key)

    assert result.status == "partial"
    assert result.report["status"] == "partial"
    quality = cast(dict[str, object], result.report["quality"])
    assert quality["corrupted_frames"] == 1
    assert record is not None
    assert record.reason == "force_rerun_requested"


def test_batch_failed_on_empty_input() -> None:
    with TemporaryDirectory() as temp_dir:
        runner = _runner(
            mission_input=MissionInput(source_uri="s", frames=[], gt_available=True),
            detector=FakeDetector(),
            statuses=JsonStatusStore(path=Path(temp_dir) / "runs.json"),
            temp_dir=temp_dir,
            engine=FakeEngine(reviewed=[]),
        )

        result = runner.run(_request(force=True))

    assert result.status == "failed"


def test_force_rerun_sets_running_reason() -> None:
    with TemporaryDirectory() as temp_dir:
        status_store = JsonStatusStore(path=Path(temp_dir) / "runs.json")
        runner = _runner(
            mission_input=MissionInput(
                source_uri="s", frames=[_frame(1, True)], gt_available=True
            ),
            detector=FakeDetector(),
            statuses=status_store,
            temp_dir=temp_dir,
            engine=FakeEngine(reviewed=[]),
        )

        runner.run(_request(force=True))
        record = status_store.get(_request(force=True).run_key)

    assert isinstance(record, RunStatusRecord)
    assert record is not None
    assert record.status in {"completed", "partial", "failed"}
    assert record.reason == "force_rerun_requested"


def test_detector_error_yields_partial_instead_of_failed() -> None:
    with TemporaryDirectory() as temp_dir:
        status_store = JsonStatusStore(path=Path(temp_dir) / "runs.json")
        runner = _runner(
            mission_input=MissionInput(
                source_uri="s",
                frames=[_frame(1, True), _frame(2, False)],
                gt_available=True,
            ),
            detector=ErrorDetector(),
            statuses=status_store,
            temp_dir=temp_dir,
            engine=FakeEngine(reviewed=[]),
        )
        result = runner.run(_request(force=True))
        record = status_store.get(_request(force=True).run_key)

    assert result.status == "partial"
    assert result.report["status"] == "partial"
    quality = cast(dict[str, object], result.report["quality"])
    assert quality["detector_error_frames"] == 2
    assert record is not None
    assert record.reason == "force_rerun_requested"


def test_partial_reason_for_corrupted_input_without_force() -> None:
    with TemporaryDirectory() as temp_dir:
        status_store = JsonStatusStore(path=Path(temp_dir) / "runs.json")
        runner = _runner(
            mission_input=MissionInput(
                source_uri="s",
                frames=[_frame(1, True, is_corrupted=True)],
                gt_available=True,
            ),
            detector=FakeDetector(),
            statuses=status_store,
            temp_dir=temp_dir,
            engine=FakeEngine(reviewed=[]),
        )
        result = runner.run(_request(force=False))
        record = status_store.get(_request(force=False).run_key)

    assert result.status == "partial"
    assert record is not None
    assert record.reason == "corrupted_input"
