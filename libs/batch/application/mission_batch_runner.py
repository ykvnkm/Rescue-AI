from __future__ import annotations

from datetime import datetime, timezone

from libs.batch.application.ports import (
    ArtifactStorePort,
    DetectionRuntimePort,
    MissionEngineFactoryPort,
    MissionEnginePort,
    MissionSourcePort,
    RunStatusStorePort,
)
from libs.batch.domain.models import (
    BatchRunRequest,
    BatchRunResult,
    DataQuality,
    FrameRecord,
    RunStatusRecord,
)
from libs.core.domain.entities import FrameEvent

# pylint: disable=too-few-public-methods,missing-class-docstring
# pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments
# pylint: disable=too-many-return-statements

PARTIAL_ERROR_RATE_THRESHOLD = 0.2


class MissionBatchRunner:
    def __init__(
        self,
        source: MissionSourcePort,
        detector: DetectionRuntimePort,
        artifacts: ArtifactStorePort,
        statuses: RunStatusStorePort,
        engine_factory: MissionEngineFactoryPort,
    ) -> None:
        self._source = source
        self._detector = detector
        self._artifacts = artifacts
        self._statuses = statuses
        self._engine_factory = engine_factory

    def run(self, request: BatchRunRequest) -> BatchRunResult:
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
            run_key=request.run_key,
            status="running",
            reason=running_reason,
        )

        try:
            mission_input = self._source.load(
                mission_id=request.mission_id, ds=request.ds
            )
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

            debug_rows: list[dict[str, object]] = []
            for frame in mission_input.frames:
                self._process_frame(
                    mission_id=mission_id,
                    frame=frame,
                    engine=engine,
                    quality=quality,
                    debug_rows=debug_rows,
                    gt_available=mission_input.gt_available,
                )

            engine.complete(
                mission_id=mission_id,
                completed_frame_id=(
                    mission_input.frames[-1].frame_id if mission_input.frames else None
                ),
            )
            report = engine.build_report(mission_id=mission_id)
            report.update(
                {
                    "mission_id_external": request.mission_id,
                    "ds": request.ds,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "quality": quality.as_dict(),
                    "status": _resolve_status(
                        report, quality, mission_input.gt_available
                    ),
                    "gt_available": mission_input.gt_available,
                    "review_status": _build_review_status(report),
                    "precision_alert": _compute_alert_precision(
                        report,
                        gt_available=mission_input.gt_available,
                    ),
                    "kpi_validity": _build_kpi_validity(mission_input.gt_available),
                }
            )

            report_uri = self._artifacts.write_report(request.run_key, report)
            debug_uri = self._artifacts.write_debug_rows(request.run_key, debug_rows)

            final_status = str(report.get("status", "completed"))
            reason = _build_reason(
                final_status,
                quality,
                forced=request.force,
            )
            self._statuses.upsert(
                run_key=request.run_key,
                status=final_status,
                reason=reason,
                report_uri=report_uri,
                debug_uri=debug_uri,
            )
            return BatchRunResult(
                run_key=request.run_key,
                status=final_status,
                report_uri=report_uri,
                debug_uri=debug_uri,
                report=report,
            )
        except Exception as exc:  # pylint: disable=broad-except
            self._statuses.upsert(
                run_key=request.run_key,
                status="failed",
                reason=str(exc),
            )
            raise

    def _process_frame(
        self,
        mission_id: str,
        frame: FrameRecord,
        engine: MissionEnginePort,
        quality: DataQuality,
        debug_rows: list[dict[str, object]],
        gt_available: bool,
    ) -> None:
        if frame.is_corrupted:
            quality.corrupted_frames += 1
            debug_rows.append(
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
        except Exception as error:  # pylint: disable=broad-except
            quality.detector_error_frames += 1
            debug_rows.append(
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
        quality.processed_frames += 1
        if not gt_available:
            quality.missing_gt_frames += 1

        frame_event = FrameEvent(
            mission_id=mission_id,
            frame_id=frame.frame_id,
            ts_sec=frame.ts_sec,
            image_uri=frame.image_uri,
            gt_person_present=frame.gt_person_present,
            gt_episode_id=None,
        )
        alerts = engine.ingest_frame(
            mission_id=mission_id, frame_event=frame_event, detections=detections
        )

        if gt_available:
            for alert in alerts:
                review_status = (
                    "reviewed_confirmed"
                    if frame.gt_person_present
                    else "reviewed_rejected"
                )
                engine.review_alert(
                    alert_id=alert.alert_id,
                    status=review_status,
                    reviewed_at_sec=frame.ts_sec,
                    reason="auto-labeled-by-gt",
                )

        debug_rows.append(
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
    if forced:
        return "force_rerun_requested"
    if status == "partial":
        if quality.detector_error_frames > 0 and quality.corrupted_frames == 0:
            return "detector_runtime_error"
        if quality.corrupted_frames > 0 and quality.detector_error_frames == 0:
            return "corrupted_input"
        return "mixed_input_and_detector_errors"
    if status == "failed":
        if quality.total_frames == 0:
            return "empty_input"
        if quality.processed_frames == 0:
            return "no_processable_frames"
    return None


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
    return existing is not None and existing.status == "completed" and not force
