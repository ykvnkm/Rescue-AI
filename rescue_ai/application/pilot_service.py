from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from rescue_ai.application.alert_policy import MissionAlertState, evaluate_alert
from rescue_ai.application.mission_metrics import (
    MissionReportData,
    build_gt_episodes,
    build_report_stats,
    episode_id_for_ts,
    split_reviewed_alerts,
)
from rescue_ai.domain.entities import (
    Alert,
    AlertRuleConfig,
    ArtifactBlob,
    Detection,
    FrameEvent,
    Mission,
)
from rescue_ai.domain.ports import (
    AlertRepository,
    ArtifactStorage,
    FrameEventRepository,
    MissionRepository,
)


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
        alert_rules: AlertRuleConfig | None = None,
    ) -> None:
        self._missions = dependencies.mission_repository
        self._alerts = dependencies.alert_repository
        self._frames = dependencies.frame_event_repository
        self._artifacts = dependencies.artifact_storage
        self._alert_state: dict[str, MissionAlertState] = {}
        self._alert_rules = alert_rules or AlertRuleConfig()
        self._report_metadata: dict[str, object] = {}

    def set_report_metadata(self, metadata: dict[str, object]) -> None:
        """Set reproducibility metadata attached to mission reports."""
        self._report_metadata = dict(metadata)

    def create_mission(
        self,
        source_name: str,
        total_frames: int,
        fps: float,
    ) -> Mission:
        mission = Mission(
            mission_id=str(uuid4()),
            source_name=source_name,
            status="created",
            created_at=_utc_now_iso(),
            total_frames=total_frames,
            fps=fps,
        )
        self._missions.create(mission)
        return mission

    def get_mission(self, mission_id: str) -> Mission | None:
        return self._missions.get(mission_id)

    def update_mission(
        self,
        mission_id: str,
        *,
        source_name: str | None = None,
        total_frames: int | None = None,
        fps: float | None = None,
    ) -> Mission | None:
        return self._missions.update_details(
            mission_id,
            source_name=source_name,
            total_frames=total_frames,
            fps=fps,
        )

    def start_mission(self, mission_id: str) -> Mission | None:
        return self._missions.update_status(mission_id=mission_id, status="running")

    def complete_mission(
        self,
        mission_id: str,
        completed_frame_id: int | None = None,
    ) -> Mission | None:
        return self._missions.update_status(
            mission_id=mission_id,
            status="completed",
            completed_frame_id=completed_frame_id,
        )

    def ingest_frame_event(
        self,
        frame_event: FrameEvent,
        detections: list[Detection],
    ) -> list[Alert]:
        mission = self._missions.get(frame_event.mission_id)
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

        stored_image_uri = self._artifacts.store_frame(
            mission_id=frame_event.mission_id,
            frame_id=frame_event.frame_id,
            source_uri=frame_event.image_uri,
        )
        frame_event.image_uri = stored_image_uri
        self._frames.add(frame_event)

        for alert in alerts:
            self._alerts.add(alert)
        return alerts

    def list_alerts(
        self,
        mission_id: str | None = None,
        status: str | None = None,
    ) -> list[Alert]:
        return self._alerts.list(mission_id=mission_id, status=status)

    def get_alert(self, alert_id: str) -> Alert | None:
        return self._alerts.get(alert_id)

    def review_alert(
        self,
        alert_id: str,
        *,
        status: str,
        reviewed_by: str | None = None,
        reviewed_at_sec: float | None = None,
        decision_reason: str | None = None,
    ) -> Alert | None:
        return self._alerts.update_status(
            alert_id=alert_id,
            status=status,
            reviewed_by=reviewed_by,
            reviewed_at_sec=reviewed_at_sec,
            decision_reason=decision_reason,
        )

    def reset_runtime_state(self) -> None:
        self._alert_state.clear()

    def get_mission_report(self, mission_id: str) -> dict[str, object]:
        mission = self._missions.get(mission_id)
        if mission is None:
            raise ValueError("Mission not found")

        if mission.status == "completed":
            cached_report = self._artifacts.load_mission_report(mission_id)
            if cached_report is not None:
                return cached_report

        report = self._build_mission_report(mission_id, mission.completed_frame_id)
        self._artifacts.save_mission_report(mission_id, report)
        return report

    def get_alert_frame_artifact(self, alert_id: str) -> ArtifactBlob:
        alert = self._alerts.get(alert_id)
        if alert is None:
            raise ValueError("Alert not found")

        artifact = self._artifacts.load_frame(alert.image_uri)
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

        report = {
            "mission_id": mission_id,
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
            self._frames.list_by_mission(mission_id),
            key=lambda item: item.frame_id,
        )
        alerts = self._alerts.list(mission_id=mission_id)
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
        mission = self._missions.get(mission_id)
        if mission is None:
            raise ValueError("Mission not found")

        frames = sorted(
            self._frames.list_by_mission(mission_id),
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
            alert_id=str(uuid4()),
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
