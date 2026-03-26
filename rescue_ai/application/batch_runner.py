"""Batch mission runner with its own DTOs and port protocols."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from rescue_ai.domain.entities import Alert, AlertRuleConfig, Detection, FrameEvent
from rescue_ai.domain.ports import ArtifactStorage, DetectorPort

PARTIAL_ERROR_RATE_THRESHOLD = 0.2


# ── Batch-specific data types ───────────────────────────────────


@dataclass(frozen=True)
class FrameRecord:
    """Single frame metadata used by batch processing."""

    frame_id: int
    ts_sec: float
    frame_path: Path
    image_uri: str
    gt_person_present: bool
    is_corrupted: bool = False


@dataclass(frozen=True)
class MissionInput:
    """Resolved mission source for a concrete processing date."""

    source_uri: str
    frames: list[FrameRecord]
    gt_available: bool


@dataclass(frozen=True)
class RunStatusRecord:
    """Persisted status snapshot for a batch run key."""

    run_key: str
    status: str
    reason: str | None = None
    report_uri: str | None = None
    debug_uri: str | None = None


@dataclass(frozen=True)
class BatchRunRequest:
    """Input command for one batch mission run."""

    mission_id: str
    ds: str
    config_hash: str
    model_version: str
    code_version: str
    alert_rules: AlertRuleConfig
    force: bool = False

    @property
    def run_key(self) -> str:
        """Unique idempotency key for this run."""
        return f"{self.mission_id}:{self.ds}:{self.config_hash}:{self.model_version}"


@dataclass
class DataQuality:
    """Frame-level quality counters used for final run status."""

    total_frames: int = 0
    processed_frames: int = 0
    corrupted_frames: int = 0
    detector_error_frames: int = 0
    missing_gt_frames: int = 0

    def as_dict(self) -> dict[str, object]:
        """Return quality metrics as a plain dictionary."""
        invalid_frames = self.corrupted_frames + self.detector_error_frames
        error_rate = (
            invalid_frames / self.total_frames if self.total_frames > 0 else 0.0
        )
        return {
            "total_frames": self.total_frames,
            "processed_frames": self.processed_frames,
            "corrupted_frames": self.corrupted_frames,
            "detector_error_frames": self.detector_error_frames,
            "missing_gt_frames": self.missing_gt_frames,
            "error_rate": round(error_rate, 4),
            "input_empty": self.total_frames == 0,
        }


@dataclass(frozen=True)
class BatchRunResult:
    """Output summary for a completed batch runner execution."""

    run_key: str
    status: str
    report_uri: str | None
    debug_uri: str | None
    report: dict[str, object] = field(default_factory=dict)


# ── Batch-specific port protocols ───────────────────────────────


class MissionSourcePort(Protocol):
    """Loads mission input frames and optional annotations for a given date."""

    def load(self, mission_id: str, ds: str) -> MissionInput: ...

    def describe_source(self) -> str: ...


class RunStatusStorePort(Protocol):
    """Stores and retrieves batch run status records."""

    def get(self, run_key: str) -> RunStatusRecord | None: ...

    def upsert(self, record: RunStatusRecord) -> None: ...


class MissionEnginePort(Protocol):
    """Mission lifecycle API used by the batch runner."""

    def create_and_start_mission(
        self,
        source_name: str,
        total_frames: int,
        fps: float,
        report_metadata: dict[str, object],
    ) -> str: ...

    def ingest_frame(
        self,
        mission_id: str,
        frame_event: FrameEvent,
        detections: list[Detection],
    ) -> list[Alert]: ...

    def review_alert(
        self,
        alert_id: str,
        status: str,
        reviewed_at_sec: float,
        reason: str,
    ) -> None: ...

    def complete(self, mission_id: str, completed_frame_id: int | None) -> None: ...

    def build_report(self, mission_id: str) -> dict[str, object]: ...


class MissionEngineFactoryPort(Protocol):
    """Builds mission engine instances for a single batch run."""

    def create(
        self,
        alert_rules: AlertRuleConfig,
        report_metadata: dict[str, object],
    ) -> MissionEnginePort: ...

    def factory_name(self) -> str: ...


# ── Runner dependencies and processing context ──────────────────


@dataclass(frozen=True)
class MissionBatchRunnerDeps:
    """Dependencies required by ``MissionBatchRunner``."""

    source: MissionSourcePort
    detector: DetectorPort
    artifacts: ArtifactStorage
    statuses: RunStatusStorePort
    engine_factory: MissionEngineFactoryPort


@dataclass
class FrameProcessContext:
    """Mutable processing context shared across frames in one run."""

    mission_id: str
    engine: MissionEnginePort
    quality: DataQuality
    debug_rows: list[dict[str, object]]
    gt_available: bool


# ── Batch runner ────────────────────────────────────────────────


class MissionBatchRunner:
    """Coordinates one idempotent batch run over mission frames."""

    def __init__(self, deps: MissionBatchRunnerDeps) -> None:
        self._source = deps.source
        self._detector = deps.detector
        self._artifacts = deps.artifacts
        self._statuses = deps.statuses
        self._engine_factory = deps.engine_factory

    def run(self, request: BatchRunRequest) -> BatchRunResult:
        """Execute a batch run, skipping if already completed and not forced."""
        existing = self._statuses.get(request.run_key)
        if (
            existing is not None
            and existing.status == "completed"
            and not request.force
        ):
            return BatchRunResult(
                run_key=request.run_key,
                status="completed",
                report_uri=existing.report_uri,
                debug_uri=existing.debug_uri,
                report={"idempotent_skip": True},
            )

        running_reason = "force_rerun_requested" if request.force else None
        self._statuses.upsert(
            RunStatusRecord(
                run_key=request.run_key,
                status="running",
                reason=running_reason,
            )
        )

        try:
            result = self._run_internal(request)
            return result
        except (RuntimeError, ValueError, OSError, TypeError, KeyError) as exc:
            self._statuses.upsert(
                RunStatusRecord(
                    run_key=request.run_key,
                    status="failed",
                    reason=str(exc),
                )
            )
            raise

    def runner_name(self) -> str:
        """Return human-readable runner name for logging."""
        return "mission-batch-runner"

    def _run_internal(self, request: BatchRunRequest) -> BatchRunResult:
        mission_input = self._source.load(mission_id=request.mission_id, ds=request.ds)
        quality = DataQuality(total_frames=len(mission_input.frames))

        report_metadata: dict[str, object] = {
            "config_hash": request.config_hash,
            "model_version": request.model_version,
            "code_version": request.code_version,
            "run_key": request.run_key,
        }
        engine = self._engine_factory.create(
            alert_rules=request.alert_rules,
            report_metadata=report_metadata,
        )

        mission_id = engine.create_and_start_mission(
            source_name=mission_input.source_uri,
            total_frames=len(mission_input.frames),
            fps=_resolve_fps(mission_input.frames),
            report_metadata=report_metadata,
        )

        context = FrameProcessContext(
            mission_id=mission_id,
            engine=engine,
            quality=quality,
            debug_rows=[],
            gt_available=mission_input.gt_available,
        )
        for frame in mission_input.frames:
            self._process_frame(frame=frame, context=context)

        engine.complete(
            mission_id=mission_id,
            completed_frame_id=(
                mission_input.frames[-1].frame_id if mission_input.frames else None
            ),
        )
        report = engine.build_report(mission_id=mission_id)
        _enrich_report(
            report=report,
            request=request,
            quality=quality,
            gt_available=mission_input.gt_available,
        )

        report_uri = self._artifacts.write_report(request.run_key, report)
        debug_uri = self._artifacts.write_debug_rows(
            request.run_key, context.debug_rows
        )
        final_status = str(report.get("status", "completed"))
        reason = _build_reason(
            status=final_status,
            quality=quality,
            forced=request.force,
        )
        self._statuses.upsert(
            RunStatusRecord(
                run_key=request.run_key,
                status=final_status,
                reason=reason,
                report_uri=report_uri,
                debug_uri=debug_uri,
            )
        )
        return BatchRunResult(
            run_key=request.run_key,
            status=final_status,
            report_uri=report_uri,
            debug_uri=debug_uri,
            report=report,
        )

    def _process_frame(
        self,
        frame: FrameRecord,
        context: FrameProcessContext,
    ) -> None:
        if frame.is_corrupted:
            context.quality.corrupted_frames += 1
            context.debug_rows.append(
                {
                    "frame_id": frame.frame_id,
                    "ts_sec": frame.ts_sec,
                    "image_uri": frame.image_uri,
                    "status": "corrupted",
                    "detections": 0,
                }
            )
            return

        try:
            detections = self._detector.detect(frame.image_uri)
        except (RuntimeError, ValueError, OSError, TypeError, KeyError) as error:
            context.quality.detector_error_frames += 1
            context.debug_rows.append(
                {
                    "frame_id": frame.frame_id,
                    "ts_sec": frame.ts_sec,
                    "image_uri": frame.image_uri,
                    "status": "detection_error",
                    "error": str(error),
                    "detections": 0,
                }
            )
            return
        context.quality.processed_frames += 1
        if not context.gt_available:
            context.quality.missing_gt_frames += 1

        frame_event = FrameEvent(
            mission_id=context.mission_id,
            frame_id=frame.frame_id,
            ts_sec=frame.ts_sec,
            image_uri=frame.image_uri,
            gt_person_present=frame.gt_person_present,
            gt_episode_id=None,
        )
        alerts = context.engine.ingest_frame(
            mission_id=context.mission_id,
            frame_event=frame_event,
            detections=detections,
        )

        if context.gt_available:
            for alert in alerts:
                review_status = (
                    "reviewed_confirmed"
                    if frame.gt_person_present
                    else "reviewed_rejected"
                )
                context.engine.review_alert(
                    alert_id=alert.alert_id,
                    status=review_status,
                    reviewed_at_sec=frame.ts_sec,
                    reason="auto-labeled-by-gt",
                )

        context.debug_rows.append(
            {
                "frame_id": frame.frame_id,
                "ts_sec": frame.ts_sec,
                "image_uri": frame.image_uri,
                "status": "processed",
                "detections": len(detections),
                "gt_person_present": frame.gt_person_present,
                "alerts_created": len(alerts),
            }
        )


# ── Private helpers ─────────────────────────────────────────────


def _resolve_status(
    report: dict[str, object],
    quality: DataQuality,
    gt_available: bool,
) -> str:
    if quality.total_frames == 0:
        return "failed"
    if quality.processed_frames == 0:
        if quality.corrupted_frames > 0 or quality.detector_error_frames > 0:
            return "partial"
        return "failed"

    error_rate = quality.as_dict()["error_rate"]
    if isinstance(error_rate, float) and error_rate > PARTIAL_ERROR_RATE_THRESHOLD:
        return "partial"

    if not gt_available:
        report["recall_event"] = None
        report["episodes_total"] = None
        report["episodes_found"] = None
        report["ttfc_sec"] = None

    return "completed"


def _build_reason(
    status: str,
    quality: DataQuality,
    forced: bool,
) -> str | None:
    reason: str | None = None
    if forced:
        reason = "force_rerun_requested"
    elif status == "partial":
        detector_errors = quality.detector_error_frames > 0
        corrupted_input = quality.corrupted_frames > 0
        if detector_errors and not corrupted_input:
            reason = "detector_runtime_error"
        elif corrupted_input and not detector_errors:
            reason = "corrupted_input"
        elif detector_errors or corrupted_input:
            reason = "mixed_input_and_detector_errors"
    elif status == "failed":
        if quality.total_frames == 0:
            reason = "empty_input"
        elif quality.processed_frames == 0:
            reason = "no_processable_frames"
    return reason


def _build_review_status(report: dict[str, object]) -> dict[str, object]:
    return {
        "alerts_total": report.get("alerts_total", 0),
        "alerts_confirmed": report.get("alerts_confirmed", 0),
        "alerts_rejected": report.get("alerts_rejected", 0),
    }


def _compute_alert_precision(
    report: dict[str, object],
    gt_available: bool,
) -> float | None:
    if not gt_available:
        return None
    alerts_total = report.get("alerts_total")
    false_total = report.get("false_alerts_total")
    if not isinstance(alerts_total, int) or alerts_total <= 0:
        return None
    if not isinstance(false_total, int):
        return None
    return round((alerts_total - false_total) / alerts_total, 4)


def _build_kpi_validity(gt_available: bool) -> dict[str, str]:
    if gt_available:
        return {
            "recall_event": "valid",
            "episodes_total": "valid",
            "episodes_found": "valid",
            "ttfc_sec": "valid",
            "precision_alert": "valid",
        }
    return {
        "recall_event": "not_applicable",
        "episodes_total": "not_applicable",
        "episodes_found": "not_applicable",
        "ttfc_sec": "not_applicable",
        "precision_alert": "not_applicable",
    }


def _resolve_fps(frames: list[FrameRecord]) -> float:
    if len(frames) < 2:
        return 1.0
    delta = frames[1].ts_sec - frames[0].ts_sec
    if delta <= 0:
        return 1.0
    return 1.0 / delta


def should_skip(existing: RunStatusRecord | None, force: bool) -> bool:
    """Check whether a run should be skipped (already completed)."""
    return existing is not None and existing.status == "completed" and not force


def _enrich_report(
    *,
    report: dict[str, object],
    request: BatchRunRequest,
    quality: DataQuality,
    gt_available: bool,
) -> None:
    report.update(
        {
            "mission_id_external": request.mission_id,
            "ds": request.ds,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "quality": quality.as_dict(),
            "status": _resolve_status(report, quality, gt_available),
            "gt_available": gt_available,
            "review_status": _build_review_status(report),
            "precision_alert": _compute_alert_precision(
                report,
                gt_available=gt_available,
            ),
            "kpi_validity": _build_kpi_validity(gt_available),
        }
    )
