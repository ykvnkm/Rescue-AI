"""Sliding-window alert policy for detection quorum."""

from __future__ import annotations

from dataclasses import dataclass, field

from rescue_ai.domain.entities import AlertRuleConfig, Detection, FrameEvent


@dataclass
class DetectionHit:
    """Single positive detection kept in alert sliding window."""

    ts_sec: float
    frame_event: FrameEvent
    detection: Detection


@dataclass
class MissionAlertState:
    """Per-mission runtime state used by alert policy."""

    recent_hits: list[DetectionHit] = field(default_factory=list)
    last_alert_ts: float | None = None
    last_positive_ts: float | None = None


@dataclass(frozen=True)
class AlertEvaluation:
    """Decision returned by alert policy for current frame."""

    positives: list[Detection]
    best_detection: Detection | None
    should_create_alert: bool
    people_detected: int


def evaluate_alert(
    frame_event: FrameEvent,
    detections: list[Detection],
    mission_state: MissionAlertState,
    rules: AlertRuleConfig,
) -> AlertEvaluation:
    current_ts = frame_event.ts_sec
    drop_expired_hits(
        mission_state=mission_state,
        current_ts=current_ts,
        window_sec=rules.window_sec,
    )

    positives = [
        item
        for item in detections
        if item.score >= rules.score_threshold and item.label == "person"
    ]
    if not positives:
        _drop_hits_after_gap(
            mission_state=mission_state, current_ts=current_ts, rules=rules
        )
        return AlertEvaluation(
            positives=[],
            best_detection=None,
            should_create_alert=False,
            people_detected=0,
        )

    _drop_hits_after_gap(
        mission_state=mission_state, current_ts=current_ts, rules=rules
    )

    best_detection = max(positives, key=lambda item: item.score)
    mission_state.recent_hits.append(
        DetectionHit(
            ts_sec=current_ts,
            frame_event=frame_event,
            detection=best_detection,
        )
    )
    mission_state.last_positive_ts = current_ts
    drop_expired_hits(
        mission_state=mission_state,
        current_ts=current_ts,
        window_sec=rules.window_sec,
    )

    if len(mission_state.recent_hits) < rules.quorum_k:
        return AlertEvaluation(
            positives=positives,
            best_detection=best_detection,
            should_create_alert=False,
            people_detected=len(positives),
        )
    if (
        mission_state.last_alert_ts is not None
        and current_ts - mission_state.last_alert_ts < rules.cooldown_sec
    ):
        return AlertEvaluation(
            positives=positives,
            best_detection=best_detection,
            should_create_alert=False,
            people_detected=len(positives),
        )

    mission_state.last_alert_ts = current_ts
    return AlertEvaluation(
        positives=positives,
        best_detection=best_detection,
        should_create_alert=True,
        people_detected=len(positives),
    )


def drop_expired_hits(
    mission_state: MissionAlertState,
    current_ts: float,
    window_sec: float,
) -> None:
    lower_bound = current_ts - window_sec
    mission_state.recent_hits = [
        hit for hit in mission_state.recent_hits if hit.ts_sec >= lower_bound
    ]


def _drop_hits_after_gap(
    mission_state: MissionAlertState,
    current_ts: float,
    rules: AlertRuleConfig,
) -> None:
    if (
        mission_state.last_positive_ts is not None
        and current_ts - mission_state.last_positive_ts > rules.gap_end_sec
    ):
        mission_state.recent_hits.clear()
