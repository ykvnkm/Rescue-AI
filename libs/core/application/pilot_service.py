from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from libs.core.application.contracts import (
    AlertRepository,
    FrameEventRepository,
    MissionRepository,
    ReviewDecision,
)
from libs.core.domain.entities import (
    Alert,
    AlertEvidence,
    AlertLifecycle,
    DetectionData,
    FrameEvent,
    Mission,
)


@dataclass(frozen=True)
class AlertRuleConfig:
    """Alert contract used for online ingestion and mission report."""

    score_threshold: float = 0.2
    window_sec: float = 1.0
    quorum_k: int = 1
    cooldown_sec: float = 1.5
    gap_end_sec: float = 1.2
    gt_gap_end_sec: float = 1.0
    match_tolerance_sec: float = 1.2


@dataclass
class DetectionInput:
    """Detection input received from frame ingestion endpoint."""

    bbox: tuple[float, float, float, float]
    score: float
    label: str = "person"
    model_name: str = "yolo8n"
    explanation: str | None = None


@dataclass
class _DetectionHit:
    ts_sec: float
    frame_event: FrameEvent
    detection: DetectionInput


@dataclass
class _MissionAlertState:
    recent_hits: list[_DetectionHit] = field(default_factory=list)
    last_alert_ts: float | None = None
    last_positive_ts: float | None = None


@dataclass(frozen=True)
class _MissionReportData:
    frames: list[FrameEvent]
    alerts: list[Alert]
    confirmed_alerts: list[Alert]
    rejected_alerts: list[Alert]


class PilotService:
    """Application service for pilot mission API."""

    def __init__(
        self,
        mission_repository: MissionRepository,
        alert_repository: AlertRepository,
        frame_event_repository: FrameEventRepository,
        alert_rules: AlertRuleConfig | None = None,
    ) -> None:
        self._missions = mission_repository
        self._alerts = alert_repository
        self._frames = frame_event_repository
        self._alert_state: dict[str, _MissionAlertState] = {}
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
        detections: list[DetectionInput],
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

        self._frames.add(frame_event)

        return self._evaluate_alert_rules(
            frame_event=frame_event, detections=detections
        )

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
        decision: ReviewDecision,
    ) -> Alert | None:
        return self._alerts.update_status(alert_id=alert_id, decision=decision)

    def reset_runtime_state(self) -> None:
        self._alert_state.clear()

    def get_mission_report(self, mission_id: str) -> dict[str, object]:
        mission = self._missions.get(mission_id)
        if mission is None:
            raise ValueError("Mission not found")

        report_data = self._collect_mission_report_data(
            mission_id=mission_id,
            completed_frame_id=mission.completed_frame_id,
        )
        report_stats = _build_report_stats(
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
    ) -> _MissionReportData:
        frames = sorted(
            self._frames.list_by_mission(mission_id),
            key=lambda item: item.frame_id,
        )
        alerts = self._alerts.list(mission_id=mission_id)
        if completed_frame_id is not None:
            frames = [item for item in frames if item.frame_id <= completed_frame_id]
            alerts = [item for item in alerts if item.frame_id <= completed_frame_id]
        confirmed_alerts, rejected_alerts = _split_reviewed_alerts(alerts)
        return _MissionReportData(
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

        episodes = _build_gt_episodes(
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
                    "episode_id": _episode_id_for_ts(frame.ts_sec, episodes),
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
        detections: list[DetectionInput],
    ) -> list[Alert]:
        mission_state = self._alert_state.setdefault(
            frame_event.mission_id,
            _MissionAlertState(),
        )
        current_ts = frame_event.ts_sec
        self._drop_expired_hits(
            mission_state=mission_state,
            current_ts=current_ts,
            window_sec=self._alert_rules.window_sec,
        )

        positives = [
            item
            for item in detections
            if item.score >= self._alert_rules.score_threshold
            and item.label == "person"
        ]
        if not positives:
            if (
                mission_state.last_positive_ts is not None
                and current_ts - mission_state.last_positive_ts
                > self._alert_rules.gap_end_sec
            ):
                mission_state.recent_hits.clear()
            return []

        if (
            mission_state.last_positive_ts is not None
            and current_ts - mission_state.last_positive_ts
            > self._alert_rules.gap_end_sec
        ):
            mission_state.recent_hits.clear()

        best_detection = max(positives, key=lambda item: item.score)
        mission_state.recent_hits.append(
            _DetectionHit(
                ts_sec=current_ts,
                frame_event=frame_event,
                detection=best_detection,
            )
        )
        mission_state.last_positive_ts = current_ts
        self._drop_expired_hits(
            mission_state=mission_state,
            current_ts=current_ts,
            window_sec=self._alert_rules.window_sec,
        )

        if len(mission_state.recent_hits) < self._alert_rules.quorum_k:
            return []
        if (
            mission_state.last_alert_ts is not None
            and current_ts - mission_state.last_alert_ts
            < self._alert_rules.cooldown_sec
        ):
            return []

        mission_state.last_alert_ts = current_ts
        return [
            self._build_alert(
                frame_event=frame_event,
                best_detection=best_detection,
                detections=positives,
                people_detected=len(positives),
            )
        ]

    @staticmethod
    def _drop_expired_hits(
        mission_state: _MissionAlertState,
        current_ts: float,
        window_sec: float,
    ) -> None:
        lower_bound = current_ts - window_sec
        mission_state.recent_hits = [
            hit for hit in mission_state.recent_hits if hit.ts_sec >= lower_bound
        ]

    def _build_alert(
        self,
        frame_event: FrameEvent,
        best_detection: DetectionInput,
        detections: list[DetectionInput],
        people_detected: int,
    ) -> Alert:
        serialized_detections = [
            DetectionData(
                bbox=item.bbox,
                score=item.score,
                label=item.label,
                model_name=item.model_name,
                explanation=item.explanation,
            )
            for item in detections
        ]
        alert = Alert(
            alert_id=str(uuid4()),
            mission_id=frame_event.mission_id,
            frame_id=frame_event.frame_id,
            ts_sec=frame_event.ts_sec,
            image_uri=frame_event.image_uri,
            evidence=AlertEvidence(
                people_detected=people_detected,
                primary_detection=DetectionData(
                    bbox=best_detection.bbox,
                    score=best_detection.score,
                    label=best_detection.label,
                    model_name=best_detection.model_name,
                    explanation=best_detection.explanation,
                ),
                detections=serialized_detections,
            ),
            lifecycle=AlertLifecycle(status="queued"),
        )
        self._alerts.add(alert)
        return alert


def _split_reviewed_alerts(alerts: list[Alert]) -> tuple[list[Alert], list[Alert]]:
    confirmed_alerts = [
        alert for alert in alerts if alert.lifecycle.status == "reviewed_confirmed"
    ]
    rejected_alerts = [
        alert for alert in alerts if alert.lifecycle.status == "reviewed_rejected"
    ]
    return confirmed_alerts, rejected_alerts


def _build_report_stats(
    report_data: _MissionReportData,
    alert_rules: AlertRuleConfig,
) -> dict[str, object]:
    episodes = _build_gt_episodes(
        frames=report_data.frames,
        gt_gap_end_sec=alert_rules.gt_gap_end_sec,
    )
    episodes_found = _count_found_episodes(
        episodes=episodes,
        alerts=report_data.alerts,
        tolerance_sec=alert_rules.match_tolerance_sec,
    )
    false_alerts_total = _count_false_alerts(
        episodes=episodes,
        alerts=report_data.alerts,
        tolerance_sec=alert_rules.match_tolerance_sec,
    )
    recall_event = episodes_found / len(episodes) if episodes else 0.0
    ttfc_sec = _compute_ttfc_first_episode(
        episodes=episodes,
        confirmed_alerts=report_data.confirmed_alerts,
        tolerance_sec=alert_rules.match_tolerance_sec,
    )

    return {
        "episodes_total": len(episodes),
        "episodes_found": episodes_found,
        "recall_event": round(recall_event, 4),
        "ttfc_sec": round(ttfc_sec, 4) if ttfc_sec is not None else None,
        "alerts_total": len(report_data.alerts),
        "alerts_confirmed": len(report_data.confirmed_alerts),
        "alerts_rejected": len(report_data.rejected_alerts),
        "false_alerts_total": false_alerts_total,
        "fp_per_minute": round(
            _compute_fp_per_minute(report_data.frames, false_alerts_total),
            4,
        ),
    }


def _build_gt_episodes(
    frames: list[FrameEvent],
    gt_gap_end_sec: float,
) -> list[tuple[float, float]]:
    episodes: list[tuple[float, float]] = []
    start_sec: float | None = None
    end_sec: float | None = None

    for frame in frames:
        if frame.gt_person_present:
            if start_sec is None:
                start_sec = frame.ts_sec
                end_sec = frame.ts_sec
                continue

            if end_sec is not None and frame.ts_sec - end_sec > gt_gap_end_sec:
                episodes.append((start_sec, end_sec))
                start_sec = frame.ts_sec
            end_sec = frame.ts_sec
            continue

        if (
            start_sec is not None
            and end_sec is not None
            and frame.ts_sec - end_sec > gt_gap_end_sec
        ):
            episodes.append((start_sec, end_sec))
            start_sec = None
            end_sec = None

    if start_sec is not None and end_sec is not None:
        episodes.append((start_sec, end_sec))
    return episodes


def _count_found_episodes(
    episodes: list[tuple[float, float]],
    alerts: list[Alert],
    tolerance_sec: float,
) -> int:
    episodes_found = 0
    for episode_start, episode_end in episodes:
        window_start = episode_start - tolerance_sec
        window_end = episode_end + tolerance_sec
        if any(window_start <= alert.ts_sec <= window_end for alert in alerts):
            episodes_found += 1
    return episodes_found


def _count_false_alerts(
    episodes: list[tuple[float, float]],
    alerts: list[Alert],
    tolerance_sec: float,
) -> int:
    false_alerts_total = 0
    for alert in alerts:
        matches_episode = any(
            (episode_start - tolerance_sec)
            <= alert.ts_sec
            <= (episode_end + tolerance_sec)
            for episode_start, episode_end in episodes
        )
        if not matches_episode:
            false_alerts_total += 1
    return false_alerts_total


def _compute_fp_per_minute(frames: list[FrameEvent], false_alerts_total: int) -> float:
    mission_duration_sec = frames[-1].ts_sec if frames else 0.0
    mission_duration_minutes = (
        mission_duration_sec / 60 if mission_duration_sec > 0 else 0
    )
    if mission_duration_minutes <= 0:
        return 0.0
    return false_alerts_total / mission_duration_minutes


def _episode_id_for_ts(
    ts_sec: float,
    episodes: list[tuple[float, float]],
) -> int | None:
    for idx, (start_sec, end_sec) in enumerate(episodes):
        if start_sec <= ts_sec <= end_sec:
            return idx + 1
    return None


def _compute_ttfc_first_episode(
    episodes: list[tuple[float, float]],
    confirmed_alerts: list[Alert],
    tolerance_sec: float,
) -> float | None:
    if not episodes:
        return None

    first_start, first_end = episodes[0]
    window_start = first_start - tolerance_sec
    window_end = first_end + tolerance_sec

    matching = [
        alert
        for alert in confirmed_alerts
        if window_start <= alert.ts_sec <= window_end
        and alert.lifecycle.reviewed_at_sec is not None
    ]
    if not matching:
        return None

    first_alert = min(matching, key=lambda item: item.ts_sec)
    reviewed_at_sec = first_alert.lifecycle.reviewed_at_sec
    if reviewed_at_sec is None:
        return None
    return reviewed_at_sec - first_start


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
