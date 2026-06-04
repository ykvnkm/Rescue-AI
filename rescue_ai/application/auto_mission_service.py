"""Application service for automatic-mode mission lifecycle (ADR-0006).

Mirrors :class:`PilotService` but runs without a human reviewer:

* ``start_auto_mission`` creates a ``Mission`` with
  :class:`MissionMode.AUTOMATIC`, persists an ``auto_mission_config``
  snapshot, and resets the injected :class:`NavigationEnginePort`.
* ``ingest_frame`` runs detection + navigation, persists the trajectory
  point (if any), re-uses the shared alert-policy, and appends an
  :class:`AutoDecision` record (``alert_created`` / ``alert_suppressed``)
  instead of queueing an operator review.
* ``complete_auto_mission`` transitions the mission to ``completed``,
  writes the ``trajectory.csv`` artifact, renders the
  ``plots/trajectory.png`` plot, and saves the ``report.json`` summary
  (all partitioned by ``ds`` so operator and automatic missions share
  one S3 layout).
* ``get_auto_mission_report`` returns the cached report (regenerates on
  demand for completed missions if the artifact is missing).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from rescue_ai.application.metrics import (
    ALERTS_CREATED_TOTAL,
    DETECTIONS_TOTAL,
    DETECTOR_ERRORS_TOTAL,
    INFERENCE_DURATION_SECONDS,
    NAVIGATION_UPDATE_DURATION_SECONDS,
    observe_duration,
)
from rescue_ai.domain.alert_policy import MissionAlertState, evaluate_alert
from rescue_ai.domain.entities import (
    Alert,
    AutoDecision,
    Detection,
    FrameEvent,
    Mission,
    TrajectoryPoint,
)
from rescue_ai.domain.ports import (
    AlertRepository,
    ArtifactStorage,
    AutoDecisionRepository,
    AutoMissionConfigRepository,
    DetectorPort,
    FrameEventRepository,
    MissionRepository,
    NavigationEnginePort,
    TrajectoryPlotRendererPort,
    TrajectoryRepository,
)
from rescue_ai.domain.value_objects import (
    AlertRuleConfig,
    ArtifactBlob,
    AutoDecisionKind,
    MissionMode,
    NavMode,
    TrajectorySource,
)

logger = logging.getLogger(__name__)


class AutoMissionServicePort(Protocol):
    """External contract of AutoMissionService for interface adapters."""

    def start_auto_mission(
        self,
        source_name: str,
        total_frames: int,
        fps: float,
        nav_mode: NavMode,
        detector_name: str,
        config_json: Mapping[str, object] | None = None,
    ) -> Mission: ...

    def ingest_frame(
        self,
        mission_id: str,
        frame_bgr: object,
        ts_sec: float,
        frame_id: int,
        image_uri: str,
        detect_enabled: bool = True,
    ) -> "AutoFrameOutcome": ...

    def complete_auto_mission(
        self,
        mission_id: str,
        completed_frame_id: int | None = None,
    ) -> Mission | None: ...

    def get_auto_mission_report(self, mission_id: str) -> dict[str, object]: ...

    def load_trajectory_plot(self, mission_id: str) -> ArtifactBlob | None: ...

    def load_trajectory_csv(self, mission_id: str) -> ArtifactBlob | None: ...


@dataclass(frozen=True)
class AutoFrameOutcome:
    """Aggregated result of processing one automatic frame."""

    detections: list[Detection]
    trajectory_point: TrajectoryPoint | None
    alerts: list[Alert]
    decisions: list[AutoDecision]


class AutoMissionService:
    """Application service for automatic missions (ADR-0006)."""

    @dataclass(frozen=True)
    class Dependencies:
        """Injected ports consumed by the automatic mission use cases."""

        mission_repository: MissionRepository
        alert_repository: AlertRepository
        frame_event_repository: FrameEventRepository
        trajectory_repository: TrajectoryRepository
        auto_decision_repository: AutoDecisionRepository
        auto_mission_config_repository: AutoMissionConfigRepository
        artifact_storage: ArtifactStorage
        detector: DetectorPort
        navigation_engine: NavigationEnginePort
        trajectory_plot_renderer: TrajectoryPlotRendererPort | None = None

    @dataclass
    class _Runtime:
        """Per-mission in-memory bookkeeping (alert window + trajectory seq)."""

        alert_state: MissionAlertState = field(default_factory=MissionAlertState)
        next_traj_seq: int = 1

    def __init__(
        self,
        dependencies: Dependencies,
        alert_rules: AlertRuleConfig,
    ) -> None:
        self._deps = dependencies
        self._alert_rules = alert_rules
        self._runtimes: dict[str, AutoMissionService._Runtime] = {}

    # ── Lifecycle ────────────────────────────────────────────────

    def start_auto_mission(
        self,
        source_name: str,
        total_frames: int,
        fps: float,
        nav_mode: NavMode,
        detector_name: str,
        config_json: Mapping[str, object] | None = None,
        mission_id: str | None = None,
        labels_payload: Mapping[str, object] | None = None,
    ) -> Mission:
        # ``mission_id`` is normally derived from ``source_name`` (content-
        # addressable identity). It can be pinned explicitly to re-run a known
        # mission in place — e.g. replaying a mission downloaded from S3.
        mission_id = mission_id or _stable_mission_id(source_name)
        existing = self._deps.mission_repository.get(mission_id)
        if existing is not None:
            # Rerun of an already-known mission (same content fed again):
            # reopen and overwrite in place rather than skip. Artifacts are
            # keyed deterministically by (ds, mission_id), so re-processing
            # overwrites frames/labels/report; the row is refreshed and
            # reset to ``running`` so a previously completed mission accepts
            # frames again. No skip-by-exists (see diplom rerun rules).
            self._deps.mission_repository.update_details(
                mission_id,
                source_name=source_name,
                total_frames=total_frames,
                fps=fps,
            )
            # Status back to ``running`` re-enables frame ingestion; the
            # ingest guard keys off ``status == "completed"`` so the stale
            # completed_frame_id is harmless and gets overwritten when this
            # rerun completes (update_status uses COALESCE — None = no-op).
            reopened = self._deps.mission_repository.update_status(
                mission_id, status="running"
            )
            self._deps.auto_mission_config_repository.save(
                mission_id=mission_id,
                nav_mode=nav_mode,
                detector=detector_name,
                config_json=dict(config_json or {}),
            )
            self._deps.navigation_engine.reset(nav_mode=nav_mode, fps=fps)
            self._runtimes[mission_id] = AutoMissionService._Runtime()
            logger.info(
                "Auto mission reopened for rerun: mission_id=%s source=%s",
                mission_id,
                source_name,
            )
            mission = reopened if reopened is not None else existing
            self._store_uploaded_labels(mission, labels_payload)
            return mission

        mission = Mission(
            mission_id=mission_id,
            source_name=source_name,
            status="running",
            created_at=_utc_now_iso(),
            total_frames=total_frames,
            fps=fps,
            mode=MissionMode.AUTOMATIC,
        )
        self._deps.mission_repository.create(mission)

        self._deps.auto_mission_config_repository.save(
            mission_id=mission.mission_id,
            nav_mode=nav_mode,
            detector=detector_name,
            config_json=dict(config_json or {}),
        )
        # ADR-0006 + diplom-prod parity: pin the engine to the
        # caller-chosen nav_mode and feed the real source FPS so the
        # marker-side ``dt`` fallback uses correct timing. NavMode.AUTO
        # keeps the legacy auto-probe behaviour.
        self._deps.navigation_engine.reset(nav_mode=nav_mode, fps=fps)
        logger.info(
            "Auto mission started: mission_id=%s source=%s fps=%.3f "
            "nav_mode=%s detector=%s",
            mission.mission_id,
            source_name,
            fps,
            nav_mode,
            detector_name,
        )
        self._runtimes[mission.mission_id] = AutoMissionService._Runtime()
        self._store_uploaded_labels(mission, labels_payload)
        return mission

    def _store_uploaded_labels(
        self, mission: Mission, labels_payload: Mapping[str, object] | None
    ) -> None:
        """Persist labels.json shipped inside an uploaded ZIP to S3.

        Keeps an offline upload symmetric with a cloud mission: frames land
        under ``{ds}/{mission_id}/frames/`` and the ground truth under
        ``{ds}/{mission_id}/labels.json``. Overwrites on re-upload (no skip).
        """
        if not labels_payload:
            return
        self._deps.artifact_storage.save_mission_annotations(
            mission_id=mission.mission_id,
            ds=_mission_ds(mission),
            payload=dict(labels_payload),
        )

    def complete_auto_mission(
        self,
        mission_id: str,
        completed_frame_id: int | None = None,
    ) -> Mission | None:
        mission = self._deps.mission_repository.get(mission_id)
        if mission is None:
            return None
        if mission.status == "completed":
            return mission
        completed = self._deps.mission_repository.update_status(
            mission_id=mission_id,
            status="completed",
            completed_frame_id=completed_frame_id,
        )
        if completed is None:
            return None

        ds = _mission_ds(completed)
        points = self._deps.trajectory_repository.list_by_mission(mission_id)
        origin = _mission_origin(
            self._deps.auto_mission_config_repository.get(mission_id)
        )
        self._deps.artifact_storage.save_trajectory_csv(
            mission_id=mission_id,
            ds=ds,
            points=points,
            origin=origin,
        )
        if self._deps.trajectory_plot_renderer is not None:
            png_bytes = self._deps.trajectory_plot_renderer.render(
                mission_id=mission_id,
                points=points,
            )
            self._deps.artifact_storage.save_trajectory_plot(
                mission_id=mission_id,
                ds=ds,
                png_bytes=png_bytes,
            )
        report = self._build_report(mission=completed, points=points)
        self._deps.artifact_storage.save_mission_report(
            mission_id=mission_id,
            ds=ds,
            report=report,
        )
        self._runtimes.pop(mission_id, None)
        return completed

    def get_auto_mission_report(self, mission_id: str) -> dict[str, object]:
        """Return the automatic-mission report, regenerating if needed.

        For a completed mission the cached ``report.json`` artifact is
        returned if present; otherwise a fresh report is built and
        persisted. Raises :class:`ValueError` if the mission does not
        exist or is still running (no report before completion).
        """
        mission = self._deps.mission_repository.get(mission_id)
        if mission is None:
            raise ValueError("Mission not found")
        if mission.mode != MissionMode.AUTOMATIC:
            raise ValueError("get_auto_mission_report requires an automatic mission")
        if mission.status != "completed":
            raise ValueError("Mission is not completed")

        ds = _mission_ds(mission)
        cached = self._deps.artifact_storage.load_mission_report(mission_id, ds)
        if cached is not None:
            return dict(cached)
        points = self._deps.trajectory_repository.list_by_mission(mission_id)
        report = self._build_report(mission=mission, points=points)
        self._deps.artifact_storage.save_mission_report(
            mission_id=mission_id, ds=ds, report=report
        )
        return report

    def load_trajectory_plot(self, mission_id: str) -> ArtifactBlob | None:
        """Load the persisted trajectory PNG for a completed auto mission."""
        mission = self._deps.mission_repository.get(mission_id)
        if mission is None:
            raise ValueError("Mission not found")
        if mission.mode != MissionMode.AUTOMATIC:
            raise ValueError("load_trajectory_plot requires an automatic mission")
        ds = _mission_ds(mission)
        return self._deps.artifact_storage.load_trajectory_plot(mission_id, ds)

    def load_trajectory_csv(self, mission_id: str) -> ArtifactBlob | None:
        """Load the persisted trajectory CSV for a completed auto mission."""
        mission = self._deps.mission_repository.get(mission_id)
        if mission is None:
            raise ValueError("Mission not found")
        if mission.mode != MissionMode.AUTOMATIC:
            raise ValueError("load_trajectory_csv requires an automatic mission")
        ds = _mission_ds(mission)
        return self._deps.artifact_storage.load_trajectory_csv(mission_id, ds)

    # ── Frame ingestion ─────────────────────────────────────────

    def ingest_frame(
        self,
        mission_id: str,
        frame_bgr: object,
        ts_sec: float,
        frame_id: int,
        image_uri: str,
        detect_enabled: bool = True,
    ) -> AutoFrameOutcome:
        mission = self._deps.mission_repository.get(mission_id)
        if mission is None:
            raise ValueError("Mission not found")
        if mission.mode != MissionMode.AUTOMATIC:
            raise ValueError("ingest_frame requires an automatic mission")
        if mission.status == "completed":
            return AutoFrameOutcome(
                detections=[], trajectory_point=None, alerts=[], decisions=[]
            )

        runtime = self._runtimes.setdefault(mission_id, AutoMissionService._Runtime())

        # Navigation MUST see the raw frame before any detector mutates
        # it (some detectors draw bboxes / overlays in-place). Keep this
        # order: navigation → detection. The detector is gated by
        # ``detect_enabled`` so the operator can disable inference (UI
        # toggle) without breaking trajectory computation.
        traj_point = self._run_navigation(
            mission_id=mission_id,
            runtime=runtime,
            frame_bgr=frame_bgr,
            ts_sec=ts_sec,
            frame_id=frame_id,
        )

        if detect_enabled:
            model_name = getattr(self._deps.detector, "model_name", "default")
            try:
                with observe_duration(INFERENCE_DURATION_SECONDS, model=model_name):
                    detections = list(self._deps.detector.detect(frame_bgr))
            except Exception:
                DETECTOR_ERRORS_TOTAL.labels(model=model_name).inc()
                raise
            for detection in detections:
                DETECTIONS_TOTAL.labels(model=model_name, label=detection.label).inc()
        else:
            detections = []

        frame_event = FrameEvent(
            mission_id=mission_id,
            frame_id=frame_id,
            ts_sec=ts_sec,
            image_uri=image_uri,
            gt_person_present=False,
            gt_episode_id=None,
        )

        # Archive EVERY frame to S3 (not only alert frames) so the mission
        # becomes a complete, re-runnable dataset for batch re-evaluation /
        # re-labelling. In-memory frames (stream / decoded video) are
        # JPEG-encoded by the storage adapter under a deterministic key
        # (frame_{id:06d}.jpg) so reruns overwrite in place.
        stored_image_uri = self._deps.artifact_storage.store_frame(
            mission_id=mission_id,
            frame_id=frame_id,
            source_uri=image_uri,
            ds=_mission_ds(mission),
            frame_bgr=frame_bgr,
        )
        frame_event.image_uri = stored_image_uri

        evaluation = evaluate_alert(
            frame_event=frame_event,
            detections=detections,
            mission_state=runtime.alert_state,
            rules=self._alert_rules,
        )

        # Persist the frame event before any alert that references it
        # (alert carries an FK to frame_event). Every frame is recorded now,
        # not only alert frames.
        self._deps.frame_event_repository.add(frame_event)

        alerts: list[Alert] = []
        decisions: list[AutoDecision] = []

        if evaluation.should_create_alert and evaluation.best_detection is not None:
            alert = Alert(
                alert_id=_stable_alert_id(mission_id=mission_id, frame_id=frame_id),
                mission_id=mission_id,
                frame_id=frame_id,
                ts_sec=ts_sec,
                image_uri=stored_image_uri,
                people_detected=evaluation.people_detected,
                primary_detection=evaluation.best_detection,
                detections=list(evaluation.positives),
            )
            self._deps.alert_repository.add(alert)
            ALERTS_CREATED_TOTAL.labels(mission_mode="auto").inc()
            alerts.append(alert)
            decisions.append(
                self._record_decision(
                    mission_id=mission_id,
                    frame_id=frame_id,
                    ts_sec=ts_sec,
                    kind=AutoDecisionKind.ALERT_CREATED,
                    reason=(
                        f"quorum reached: positives={len(evaluation.positives)} "
                        f"score={evaluation.best_detection.score:.3f}"
                    ),
                )
            )
        elif evaluation.positives:
            decisions.append(
                self._record_decision(
                    mission_id=mission_id,
                    frame_id=frame_id,
                    ts_sec=ts_sec,
                    kind=AutoDecisionKind.ALERT_SUPPRESSED,
                    reason=(
                        f"quorum/cooldown not satisfied: "
                        f"positives={len(evaluation.positives)}"
                    ),
                )
            )

        return AutoFrameOutcome(
            detections=detections,
            trajectory_point=traj_point,
            alerts=alerts,
            decisions=decisions,
        )

    # ── Helpers ─────────────────────────────────────────────────

    def _run_navigation(
        self,
        *,
        mission_id: str,
        runtime: _Runtime,
        frame_bgr: object,
        ts_sec: float,
        frame_id: int,
    ) -> TrajectoryPoint | None:
        with observe_duration(NAVIGATION_UPDATE_DURATION_SECONDS):
            raw_point = self._deps.navigation_engine.step(
                frame_bgr=frame_bgr, ts_sec=ts_sec, frame_id=frame_id
            )
        if raw_point is None:
            return None
        point = TrajectoryPoint(
            mission_id=mission_id,
            seq=runtime.next_traj_seq,
            ts_sec=raw_point.ts_sec,
            frame_id=raw_point.frame_id,
            x=raw_point.x,
            y=raw_point.y,
            z=raw_point.z,
            source=raw_point.source,
        )
        runtime.next_traj_seq += 1
        self._deps.trajectory_repository.add(point)
        return point

    def _record_decision(
        self,
        *,
        mission_id: str,
        frame_id: int,
        ts_sec: float,
        kind: AutoDecisionKind,
        reason: str,
    ) -> AutoDecision:
        decision = AutoDecision(
            decision_id=_stable_decision_id(
                mission_id=mission_id, frame_id=frame_id, kind=kind
            ),
            mission_id=mission_id,
            frame_id=frame_id,
            ts_sec=ts_sec,
            kind=kind,
            reason=reason,
            created_at=_utc_now_iso(),
        )
        self._deps.auto_decision_repository.add(decision)
        return decision

    def _build_report(
        self,
        *,
        mission: Mission,
        points: list[TrajectoryPoint],
    ) -> dict[str, object]:
        """Build the automatic-mission JSON report.

        Fields mirror the operator report where meaningful (``mission_id``,
        ``mode``, ``status``, timestamps, frame counters) and add the
        automatic-specific breakdown: trajectory stats by
        :class:`TrajectorySource`, alert-created vs alert-suppressed
        decision counts, and the snapshot of the navigation config.
        """
        mission_id = mission.mission_id
        frames = self._deps.frame_event_repository.list_by_mission(mission_id)
        alerts = self._deps.alert_repository.list(mission_id=mission_id)
        decisions = self._deps.auto_decision_repository.list_by_mission(mission_id)
        config_snapshot = self._deps.auto_mission_config_repository.get(mission_id)
        origin = _mission_origin(config_snapshot)

        source_counts: dict[str, int] = {str(source): 0 for source in TrajectorySource}
        for point in points:
            source_counts[str(point.source)] = (
                source_counts.get(str(point.source), 0) + 1
            )

        decision_counts = {
            str(AutoDecisionKind.ALERT_CREATED): 0,
            str(AutoDecisionKind.ALERT_SUPPRESSED): 0,
        }
        for decision in decisions:
            decision_counts[str(decision.kind)] = (
                decision_counts.get(str(decision.kind), 0) + 1
            )

        ds = _mission_ds(mission)
        duration_sec = 0.0
        if points:
            duration_sec = max(point.ts_sec for point in points) - min(
                point.ts_sec for point in points
            )
        elif frames:
            duration_sec = max(frame.ts_sec for frame in frames) - min(
                frame.ts_sec for frame in frames
            )

        report: dict[str, object] = {
            "mission_id": mission_id,
            "mode": str(MissionMode.AUTOMATIC),
            "status": mission.status,
            "created_at": mission.created_at,
            "completed_frame_id": mission.completed_frame_id,
            "source_name": mission.source_name,
            "fps": mission.fps,
            "total_frames_declared": mission.total_frames,
            "frames_processed": len(frames),
            "alerts_total": len(alerts),
            "decisions": decision_counts,
            "trajectory": {
                "points_total": len(points),
                "by_source": source_counts,
                "duration_sec": round(duration_sec, 3),
                # When an absolute start point was given, the trajectory is
                # reported geographically (lat/lon in trajectory.csv); else it
                # stays relative-to-origin in meters.
                "coordinate_system": ("absolute" if origin is not None else "relative"),
                "origin": (
                    {"lat": origin[0], "lon": origin[1]} if origin is not None else None
                ),
            },
            "artifacts": {
                "trajectory_csv": (f"{ds}/{mission_id}/trajectory.csv"),
                "trajectory_plot": (
                    f"{ds}/{mission_id}/plots/trajectory.png"
                    if self._deps.trajectory_plot_renderer is not None
                    else None
                ),
                "report_json": f"{ds}/{mission_id}/report.json",
            },
            "config_snapshot": dict(config_snapshot) if config_snapshot else {},
            "generated_at": _utc_now_iso(),
        }
        return report


def _mission_origin(
    config: Mapping[str, object] | None,
) -> tuple[float, float] | None:
    """Read an optional absolute start point ``(lat, lon)`` from mission config.

    Accepts either the raw ``config_json`` or the repository snapshot that
    nests it under a ``config_json`` key.
    """
    if not isinstance(config, Mapping):
        return None
    inner = config.get("config_json")
    source = inner if isinstance(inner, Mapping) else config
    try:
        return (
            float(source["origin_lat"]),
            float(source["origin_lon"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mission_ds(mission: Mission) -> str:
    created_at = (mission.created_at or "").strip()
    if len(created_at) < 10:
        raise ValueError(
            f"Mission {mission.mission_id} has no usable created_at: {created_at!r}"
        )
    return created_at[:10]


def _stable_mission_id(source_name: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"rescue-ai/auto-mission/{source_name}"))


def _stable_alert_id(mission_id: str, frame_id: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"rescue-ai/auto-alert/{mission_id}/{frame_id}"))


def _stable_decision_id(mission_id: str, frame_id: int, kind: AutoDecisionKind) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"rescue-ai/auto-decision/{mission_id}/{frame_id}/{kind}",
        )
    )
