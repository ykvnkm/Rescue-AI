from __future__ import annotations

from dataclasses import dataclass

from libs.core.application.models import AlertRuleConfig
from libs.core.domain.entities import Alert, FrameEvent


@dataclass(frozen=True)
class MissionReportData:
    """Aggregated mission data required to compute report metrics."""

    frames: list[FrameEvent]
    alerts: list[Alert]
    confirmed_alerts: list[Alert]
    rejected_alerts: list[Alert]


def split_reviewed_alerts(alerts: list[Alert]) -> tuple[list[Alert], list[Alert]]:
    confirmed_alerts = [
        alert for alert in alerts if alert.lifecycle.status == "reviewed_confirmed"
    ]
    rejected_alerts = [
        alert for alert in alerts if alert.lifecycle.status == "reviewed_rejected"
    ]
    return confirmed_alerts, rejected_alerts


def build_report_stats(
    report_data: MissionReportData,
    alert_rules: AlertRuleConfig,
) -> dict[str, object]:
    episodes = build_gt_episodes(
        frames=report_data.frames,
        gt_gap_end_sec=alert_rules.gt_gap_end_sec,
    )
    episodes_found = count_found_episodes(
        episodes=episodes,
        alerts=report_data.alerts,
        tolerance_sec=alert_rules.match_tolerance_sec,
    )
    false_alerts_total = count_false_alerts(
        episodes=episodes,
        alerts=report_data.alerts,
        tolerance_sec=alert_rules.match_tolerance_sec,
    )
    recall_event = episodes_found / len(episodes) if episodes else 0.0
    ttfc_sec = compute_ttfc_first_episode(
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
            compute_fp_per_minute(report_data.frames, false_alerts_total),
            4,
        ),
    }


def build_gt_episodes(
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


def count_found_episodes(
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


def count_false_alerts(
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


def compute_fp_per_minute(frames: list[FrameEvent], false_alerts_total: int) -> float:
    mission_duration_sec = frames[-1].ts_sec if frames else 0.0
    mission_duration_minutes = (
        mission_duration_sec / 60 if mission_duration_sec > 0 else 0
    )
    if mission_duration_minutes <= 0:
        return 0.0
    return false_alerts_total / mission_duration_minutes


def episode_id_for_ts(
    ts_sec: float,
    episodes: list[tuple[float, float]],
) -> int | None:
    for idx, (start_sec, end_sec) in enumerate(episodes):
        if start_sec <= ts_sec <= end_sec:
            return idx + 1
    return None


def compute_ttfc_first_episode(
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
