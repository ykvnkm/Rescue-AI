from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, cast
from uuid import NAMESPACE_URL, uuid5

from rescue_ai.domain.alert_policy import MissionAlertState, evaluate_alert
from rescue_ai.domain.entities import Alert, Detection, FrameEvent, Mission
from rescue_ai.domain.mission_metrics import (
    MissionReportData,
    build_gt_episodes,
    build_report_stats,
    episode_id_for_ts,
    split_reviewed_alerts,
)
from rescue_ai.domain.ports import (
    AlertRepository,
    AlertReviewPayload,
    ArtifactStorage,
    FrameEventRepository,
    MissionRepository,
    ReportMetadataPayload,
)
from rescue_ai.domain.value_objects import AlertRuleConfig, ArtifactBlob


class PilotServicePort(Protocol):
    """External contract of PilotService for infrastructure adapters."""

    def set_report_metadata(self, payload: ReportMetadataPayload) -> None: ...

    def create_mission(self, source_name: str, total_frames: int, fps: float): ...

    def start_mission(self, mission_id: str): ...

    def ingest_frame_event(
        self,
        frame_event: FrameEvent,
        detections: list[Detection],
    ) -> list[Alert]: ...

    def review_alert(self, alert_id: str, updates: AlertReviewPayload): ...

    def complete_mission(
        self,
        mission_id: str,
        completed_frame_id: int | None,
    ): ...

    def get_mission_report(self, mission_id: str) -> dict[str, object]: ...


class PilotService:
    """Application service for pilot mission API."""

    @dataclass(frozen=True)
    class Dependencies:
        """Injected persistence and artifact ports for use-case orchestration."""

        mission_repository: MissionRepository
        alert_repository: AlertRepository
        frame_event_repository: FrameEventRepository
        artifact_storage: ArtifactStorage

    def __init__(
        self,
        dependencies: Dependencies,
        alert_rules: AlertRuleConfig,
    ) -> None:
        """Initialise service with injected dependencies and alert rules."""
        self._deps = dependencies
        self._alert_state: dict[str, MissionAlertState] = {}
        self._alert_rules = alert_rules
        self._report_metadata: ReportMetadataPayload = {}

    def set_report_metadata(self, metadata: ReportMetadataPayload) -> None:
        """Set reproducibility metadata attached to mission reports."""
        self._report_metadata = cast(ReportMetadataPayload, dict(metadata))

    def create_mission(
        self,
        source_name: str,
        total_frames: int,
        fps: float,
    ) -> Mission:
        """Create a new mission and persist it.

        The repository auto-generates a human-readable slug
        (e.g. ``2026-03-28/mission-1``) based on the creation date.
        """
        mission_id = _stable_mission_id(source_name)
        existing = self._deps.mission_repository.get(mission_id)
        if existing is not None:
            updated = self._deps.mission_repository.update_details(
                mission_id,
                source_name=source_name,
                total_frames=total_frames,
                fps=fps,
            )
            return updated if updated is not None else existing

        mission = Mission(
            mission_id=mission_id,
            source_name=source_name,
            status="created",
            created_at=_utc_now_iso(),
            total_frames=total_frames,
            fps=fps,
        )
        self._deps.mission_repository.create(mission)
        # Register slug in artifact storage so S3 paths use it
        if mission.slug and hasattr(self._deps.artifact_storage, "register_slug"):
            self._deps.artifact_storage.register_slug(mission.mission_id, mission.slug)
        return mission

    def get_mission(self, mission_id: str) -> Mission | None:
        """Return a mission by ID, or None if not found."""
        return self._deps.mission_repository.get(mission_id)

    def update_mission(
        self,
        mission_id: str,
        *,
        source_name: str | None = None,
        total_frames: int | None = None,
        fps: float | None = None,
    ) -> Mission | None:
        """Update mutable mission details (name, frames, fps)."""
        return self._deps.mission_repository.update_details(
            mission_id,
            source_name=source_name,
            total_frames=total_frames,
            fps=fps,
        )

    def start_mission(self, mission_id: str) -> Mission | None:
        """Transition a mission to the running state."""
        return self._deps.mission_repository.update_status(
            mission_id=mission_id, status="running"
        )

    def complete_mission(
        self,
        mission_id: str,
        completed_frame_id: int | None = None,
    ) -> Mission | None:
        """Mark a mission as completed, optionally recording the last frame."""
        return self._deps.mission_repository.update_status(
            mission_id=mission_id,
            status="completed",
            completed_frame_id=completed_frame_id,
        )

    def ingest_frame_event(
        self,
        frame_event: FrameEvent,
        detections: list[Detection],
    ) -> list[Alert]:
        """Process a frame event, evaluate alert rules, and persist results."""
        mission = self._deps.mission_repository.get(frame_event.mission_id)
        if mission is None:
            raise ValueError("Mission not found")
        if (
            mission.status == "completed"
            and mission.completed_frame_id is not None
            and frame_event.frame_id > mission.completed_frame_id
        ):
            return []

        alerts = self._evaluate_alert_rules(
            frame_event=frame_event,
            detections=detections,
        )

        stored_image_uri = frame_event.image_uri
        if alerts:
            stored_image_uri = self._deps.artifact_storage.store_frame(
                mission_id=frame_event.mission_id,
                frame_id=frame_event.frame_id,
                source_uri=frame_event.image_uri,
            )
        frame_event.image_uri = stored_image_uri
        self._deps.frame_event_repository.add(frame_event)

        for alert in alerts:
            alert.image_uri = stored_image_uri
            self._deps.alert_repository.add(alert)
        return alerts

    def list_alerts(
        self,
        mission_id: str | None = None,
        status: str | None = None,
    ) -> list[Alert]:
        return self._deps.alert_repository.list(mission_id=mission_id, status=status)

    def get_alert(self, alert_id: str) -> Alert | None:
        return self._deps.alert_repository.get(alert_id)

    def review_alert(
        self,
        alert_id: str,
        updates: AlertReviewPayload,
    ) -> Alert | None:
        """Apply a review decision to an alert."""
        return self._deps.alert_repository.update_status(alert_id, updates)

    def reset_runtime_state(self) -> None:
        self._alert_state.clear()

    def get_mission_report(self, mission_id: str) -> dict[str, object]:
        mission = self._deps.mission_repository.get(mission_id)
        if mission is None:
            raise ValueError("Mission not found")

        if mission.status == "completed":
            cached_report = self._deps.artifact_storage.load_mission_report(mission_id)
            if cached_report is not None:
                return dict(cached_report)

        report = self._build_mission_report(mission_id, mission.completed_frame_id)
        self._deps.artifact_storage.save_mission_report(mission_id, report)
        return report

    def get_alert_frame_artifact(self, alert_id: str) -> ArtifactBlob:
        alert = self._deps.alert_repository.get(alert_id)
        if alert is None:
            raise ValueError("Alert not found")

        artifact = self._deps.artifact_storage.load_frame(alert.image_uri)
        if artifact is None:
            raise FileNotFoundError("Frame artifact not found")
        return artifact

    def _build_mission_report(
        self,
        mission_id: str,
        completed_frame_id: int | None,
    ) -> dict[str, object]:
        report_data = self._collect_mission_report_data(
            mission_id=mission_id,
            completed_frame_id=completed_frame_id,
        )
        report_stats = build_report_stats(
            report_data=report_data,
            alert_rules=self._alert_rules,
        )
        gt_available = any(frame.gt_person_present for frame in report_data.frames)
        report_stats["kpi_validity"] = (
            {
                "recall_event": "valid",
                "episodes_total": "valid",
                "episodes_found": "valid",
                "ttfc_sec": "valid",
                "false_alerts_total": "valid",
                "fp_per_minute": "valid",
            }
            if gt_available
            else {
                "recall_event": "not_applicable",
                "episodes_total": "not_applicable",
                "episodes_found": "not_applicable",
                "ttfc_sec": "not_applicable",
                "false_alerts_total": "not_applicable",
                "fp_per_minute": "not_applicable",
            }
        )
        if not gt_available:
            report_stats["episodes_total"] = None
            report_stats["episodes_found"] = None
            report_stats["recall_event"] = None
            report_stats["ttfc_sec"] = None
            report_stats["false_alerts_total"] = None
            report_stats["fp_per_minute"] = None

        report = {
            "mission_id": mission_id,
            "gt_available": gt_available,
            **report_stats,
            "generated_at": _utc_now_iso(),
        }
        report.update(self._report_metadata)
        return report

    def _collect_mission_report_data(
        self,
        mission_id: str,
        completed_frame_id: int | None,
    ) -> MissionReportData:
        frames = sorted(
            self._deps.frame_event_repository.list_by_mission(mission_id),
            key=lambda item: item.frame_id,
        )
        alerts = self._deps.alert_repository.list(mission_id=mission_id)
        if completed_frame_id is not None:
            frames = [item for item in frames if item.frame_id <= completed_frame_id]
            alerts = [item for item in alerts if item.frame_id <= completed_frame_id]
        confirmed_alerts, rejected_alerts = split_reviewed_alerts(alerts)
        return MissionReportData(
            frames=frames,
            alerts=alerts,
            confirmed_alerts=confirmed_alerts,
            rejected_alerts=rejected_alerts,
        )

    def get_mission_episode_debug(
        self,
        mission_id: str,
        limit: int = 200,
    ) -> dict[str, object]:
        mission = self._deps.mission_repository.get(mission_id)
        if mission is None:
            raise ValueError("Mission not found")

        frames = sorted(
            self._deps.frame_event_repository.list_by_mission(mission_id),
            key=lambda item: item.frame_id,
        )
        cutoff = mission.completed_frame_id
        if cutoff is not None:
            frames = [item for item in frames if item.frame_id <= cutoff]

        episodes = build_gt_episodes(
            frames=frames,
            gt_gap_end_sec=self._alert_rules.gt_gap_end_sec,
        )
        rows: list[dict[str, object]] = []
        capped_limit = max(1, int(limit))
        for frame in frames[:capped_limit]:
            rows.append(
                {
                    "frame_id": frame.frame_id,
                    "ts_sec": frame.ts_sec,
                    "gt_person_present": frame.gt_person_present,
                    "episode_id": episode_id_for_ts(frame.ts_sec, episodes),
                    "image_uri": frame.image_uri,
                }
            )

        return {
            "mission_id": mission_id,
            "completed_frame_id": cutoff,
            "frames_total_for_report": len(frames),
            "gt_gap_end_sec": self._alert_rules.gt_gap_end_sec,
            "episodes_total": len(episodes),
            "episodes": [
                {
                    "episode_id": idx + 1,
                    "start_sec": episode[0],
                    "end_sec": episode[1],
                }
                for idx, episode in enumerate(episodes)
            ],
            "rows_limit": capped_limit,
            "rows": rows,
        }

    def _evaluate_alert_rules(
        self,
        frame_event: FrameEvent,
        detections: list[Detection],
    ) -> list[Alert]:
        mission_state = self._alert_state.setdefault(
            frame_event.mission_id,
            MissionAlertState(),
        )
        evaluation = evaluate_alert(
            frame_event=frame_event,
            detections=detections,
            mission_state=mission_state,
            rules=self._alert_rules,
        )
        if not evaluation.should_create_alert or evaluation.best_detection is None:
            return []

        return [
            self._build_alert(
                frame_event=frame_event,
                best_detection=evaluation.best_detection,
                detections=evaluation.positives,
                people_detected=evaluation.people_detected,
            )
        ]

    def _build_alert(
        self,
        frame_event: FrameEvent,
        best_detection: Detection,
        detections: list[Detection],
        people_detected: int,
    ) -> Alert:
        alert = Alert(
            alert_id=_stable_alert_id(
                mission_id=frame_event.mission_id,
                frame_id=frame_event.frame_id,
            ),
            mission_id=frame_event.mission_id,
            frame_id=frame_event.frame_id,
            ts_sec=frame_event.ts_sec,
            image_uri=frame_event.image_uri,
            people_detected=people_detected,
            primary_detection=best_detection,
            detections=list(detections),
        )
        return alert


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_mission_id(source_name: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"rescue-ai/mission/{source_name}"))


def _stable_alert_id(mission_id: str, frame_id: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"rescue-ai/alert/{mission_id}/{frame_id}"))
