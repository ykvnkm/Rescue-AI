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
    AlertLifecycle,
    DetectionData,
    FrameEvent,
    Mission,
)

DEFAULT_SCORE_THRESHOLD = 0.2
ALERT_WINDOW_SEC = 1.0
ALERT_QUORUM_K = 2
ALERT_COOLDOWN_SEC = 2.0
ALERT_GAP_END_SEC = 1.0


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


class PilotService:
    """Application service for pilot mission API."""

    def __init__(
        self,
        mission_repository: MissionRepository,
        alert_repository: AlertRepository,
        frame_event_repository: FrameEventRepository,
    ) -> None:
        self._missions = mission_repository
        self._alerts = alert_repository
        self._frames = frame_event_repository
        self._alert_state: dict[str, _MissionAlertState] = {}

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

    def complete_mission(self, mission_id: str) -> Mission | None:
        return self._missions.update_status(mission_id=mission_id, status="completed")

    def ingest_frame_event(
        self,
        frame_event: FrameEvent,
        detections: list[DetectionInput],
    ) -> list[Alert]:
        if self._missions.get(frame_event.mission_id) is None:
            raise ValueError("Mission not found")

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
        if self._missions.get(mission_id) is None:
            raise ValueError("Mission not found")

        frames = sorted(
            self._frames.list_by_mission(mission_id),
            key=lambda item: item.frame_id,
        )
        alerts = self._alerts.list(mission_id=mission_id)
        confirmed_alerts, rejected_alerts = _split_reviewed_alerts(alerts)

        episodes = _build_gt_episodes(frames)
        episodes_found, ttfc_candidates = _collect_episode_coverage(
            episodes=episodes,
            confirmed_alerts=confirmed_alerts,
        )

        mission_duration_sec = frames[-1].ts_sec if frames else 0.0
        mission_duration_hours = (
            mission_duration_sec / 3600 if mission_duration_sec > 0 else 0
        )
        fp_per_hour = (
            len(rejected_alerts) / mission_duration_hours
            if mission_duration_hours
            else 0.0
        )

        recall_event = episodes_found / len(episodes) if episodes else 0.0
        ttfc_sec = min(ttfc_candidates) if ttfc_candidates else None

        return {
            "mission_id": mission_id,
            "episodes_total": len(episodes),
            "episodes_found": episodes_found,
            "recall_event": round(recall_event, 4),
            "ttfc_sec": round(ttfc_sec, 4) if ttfc_sec is not None else None,
            "alerts_total": len(alerts),
            "alerts_confirmed": len(confirmed_alerts),
            "alerts_rejected": len(rejected_alerts),
            "false_alerts_total": len(rejected_alerts),
            "fp_per_hour": round(fp_per_hour, 4),
            "generated_at": _utc_now_iso(),
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
        self._drop_expired_hits(mission_state=mission_state, current_ts=current_ts)

        positives = [
            item
            for item in detections
            if item.score >= DEFAULT_SCORE_THRESHOLD and item.label == "person"
        ]
        if not positives:
            if (
                mission_state.last_positive_ts is not None
                and current_ts - mission_state.last_positive_ts > ALERT_GAP_END_SEC
            ):
                mission_state.recent_hits.clear()
            return []

        if (
            mission_state.last_positive_ts is not None
            and current_ts - mission_state.last_positive_ts > ALERT_GAP_END_SEC
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
        self._drop_expired_hits(mission_state=mission_state, current_ts=current_ts)

        if len(mission_state.recent_hits) < ALERT_QUORUM_K:
            return []
        if (
            mission_state.last_alert_ts is not None
            and current_ts - mission_state.last_alert_ts < ALERT_COOLDOWN_SEC
        ):
            return []

        mission_state.last_alert_ts = current_ts
        return [self._build_alert(frame_event=frame_event, detection=best_detection)]

    @staticmethod
    def _drop_expired_hits(
        mission_state: _MissionAlertState,
        current_ts: float,
    ) -> None:
        lower_bound = current_ts - ALERT_WINDOW_SEC
        mission_state.recent_hits = [
            hit for hit in mission_state.recent_hits if hit.ts_sec >= lower_bound
        ]

    def _build_alert(
        self,
        frame_event: FrameEvent,
        detection: DetectionInput,
    ) -> Alert:
        alert = Alert(
            alert_id=str(uuid4()),
            mission_id=frame_event.mission_id,
            frame_id=frame_event.frame_id,
            ts_sec=frame_event.ts_sec,
            image_uri=frame_event.image_uri,
            detection=DetectionData(
                bbox=detection.bbox,
                score=detection.score,
                label=detection.label,
                model_name=detection.model_name,
                explanation=detection.explanation,
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


def _build_gt_episodes(frames: list[FrameEvent]) -> list[tuple[float, float]]:
    episodes: list[tuple[float, float]] = []
    start_sec: float | None = None
    end_sec: float | None = None

    for frame in frames:
        if frame.gt_person_present:
            if start_sec is None:
                start_sec = frame.ts_sec
            end_sec = frame.ts_sec
            continue

        if start_sec is not None and end_sec is not None:
            episodes.append((start_sec, end_sec))
            start_sec = None
            end_sec = None

    if start_sec is not None and end_sec is not None:
        episodes.append((start_sec, end_sec))
    return episodes


def _collect_episode_coverage(
    episodes: list[tuple[float, float]],
    confirmed_alerts: list[Alert],
) -> tuple[int, list[float]]:
    episodes_found = 0
    ttfc_candidates: list[float] = []

    for episode_start, episode_end in episodes:
        episode_alerts = [
            alert
            for alert in confirmed_alerts
            if episode_start <= alert.ts_sec <= episode_end
        ]
        if not episode_alerts:
            continue

        episodes_found += 1
        reviewed_times = [
            alert.lifecycle.reviewed_at_sec
            for alert in episode_alerts
            if alert.lifecycle.reviewed_at_sec is not None
        ]
        if reviewed_times:
            ttfc_candidates.append(min(reviewed_times) - episode_start)
    return episodes_found, ttfc_candidates


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
