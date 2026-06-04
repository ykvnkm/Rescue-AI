"""Mission-level metric aggregation and KPI computation.

В модуле живут два набора правил расчёта метрик (см. таблицу 3.1
пояснительной записки):
  * KPI миссии (recall_event, TtFC, fp_per_minute) — секции
    ``build_report_stats`` и далее;
  * Индексы дрейфа PSI / CSI по формулам (2.11)–(2.12) пояснительной
    записки — секция в конце файла.
Drift-функции отделены от KPI и не зависят от классов Mission/Alert —
они работают на чистых числовых распределениях.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import NamedTuple, Sequence

from rescue_ai.domain.entities import Alert, FrameEvent
from rescue_ai.domain.value_objects import AlertRuleConfig, AlertStatus


class MissionReportData(NamedTuple):
    """Aggregated mission data required to compute report metrics."""

    frames: list[FrameEvent]
    alerts: list[Alert]
    confirmed_alerts: list[Alert]
    rejected_alerts: list[Alert]


def split_reviewed_alerts(alerts: list[Alert]) -> tuple[list[Alert], list[Alert]]:
    """Split alerts into confirmed and rejected lists based on review status."""
    confirmed_alerts = [
        alert for alert in alerts if alert.status == AlertStatus.REVIEWED_CONFIRMED
    ]
    rejected_alerts = [
        alert for alert in alerts if alert.status == AlertStatus.REVIEWED_REJECTED
    ]
    return confirmed_alerts, rejected_alerts


def build_report_stats(
    report_data: MissionReportData,
    alert_rules: AlertRuleConfig,
) -> dict[str, object]:
    """Build a dictionary of mission KPI statistics from report data."""
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
    """Build ground-truth person-presence episodes from frame events."""
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
    """Count ground-truth episodes matched by at least one alert."""
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
    """Count alerts that do not match any ground-truth episode."""
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
        and alert.reviewed_at_sec is not None
    ]
    if not matching:
        return None

    first_alert = min(matching, key=lambda item: item.ts_sec)
    if first_alert.reviewed_at_sec is None:
        return None
    return first_alert.reviewed_at_sec - first_start


# ── Дрейф данных (PSI/CSI, формулы (2.11)-(2.12) пояснительной записки)

# Учебная норма: 10 бинов на признак. Меньше — теряем чувствительность;
# больше — sparse-buckets шумят даже на ~1000 кадров. PSI/CSI в индустрии
# традиционно считают на 10 бинах.
DRIFT_BINS: int = 10

# Confidence модели и среднее brightness кадра живут в [0, 1].
_UNIT_INTERVAL_EDGES: tuple[float, ...] = tuple(
    round(i / DRIFT_BINS, 4) for i in range(DRIFT_BINS + 1)
)
CONFIDENCE_EDGES: tuple[float, ...] = _UNIT_INTERVAL_EDGES
BRIGHTNESS_EDGES: tuple[float, ...] = _UNIT_INTERVAL_EDGES

# Bbox area — доля площади кадра. Для дронов > 2 % почти не бывает,
# поэтому первая половина бинов сжата, последняя растянута.
BBOX_AREA_EDGES: tuple[float, ...] = (
    0.0,
    0.001,
    0.002,
    0.005,
    0.01,
    0.02,
    0.05,
    0.1,
    0.2,
    0.35,
    1.0,
)

# Aspect ratio bbox = width / height. Стоящий человек ~0.3-0.6,
# лежащий ~1.5-3.0; крайние бины ловят выбросы.
BBOX_RATIO_EDGES: tuple[float, ...] = (
    0.0,
    0.2,
    0.4,
    0.6,
    0.8,
    1.0,
    1.5,
    2.0,
    3.0,
    5.0,
    100.0,
)


@dataclass(frozen=True)
class FrameFeatures:
    """Per-frame набор признаков для гистограмм дрейфа.

    Кадры без детекций имеют ``confidence_max=0.0`` и ``bbox_*=None``;
    brightness — свойство самого кадра, всегда заполняется.
    """

    confidence_max: float
    bbox_area_norm: float | None
    bbox_ratio: float | None
    brightness_mean: float


@dataclass(frozen=True)
class FeatureHistograms:
    """4 нормализованные гистограммы + общее n_samples."""

    confidence: tuple[float, ...]
    bbox_area: tuple[float, ...]
    bbox_ratio: tuple[float, ...]
    brightness: tuple[float, ...]
    n_samples: int

    def is_empty(self) -> bool:
        return self.n_samples == 0


@dataclass(frozen=True)
class DriftScores:
    """Итог сравнения current гистограмм с reference."""

    psi_confidence: float
    csi_bbox_area: float
    csi_bbox_ratio: float
    csi_brightness: float

    def drift_flag(self, threshold: float = 0.2) -> bool:
        """Формула (2.12) пояснительной записки.

        Дрейф фиксируется, если хотя бы один индикатор превышает
        порог. По умолчанию порог = 0.2 (учебное значение «moderate
        shift»).
        """
        return (
            self.psi_confidence > threshold
            or self.csi_bbox_area > threshold
            or self.csi_bbox_ratio > threshold
            or self.csi_brightness > threshold
        )


def build_histograms(features: Sequence[FrameFeatures]) -> FeatureHistograms:
    """Собрать per-frame features в нормализованные гистограммы.

    bbox features учитываются только когда bbox есть; brightness и
    confidence — всегда (None для confidence не бывает, frame без
    детекций даёт 0.0).
    """
    if not features:
        return FeatureHistograms(
            confidence=_zero_hist(),
            bbox_area=_zero_hist(),
            bbox_ratio=_zero_hist(),
            brightness=_zero_hist(),
            n_samples=0,
        )

    conf_values = [float(f.confidence_max) for f in features]
    brightness_values = [float(f.brightness_mean) for f in features]
    area_values = [
        float(f.bbox_area_norm) for f in features if f.bbox_area_norm is not None
    ]
    ratio_values = [float(f.bbox_ratio) for f in features if f.bbox_ratio is not None]

    return FeatureHistograms(
        confidence=_histogram(conf_values, CONFIDENCE_EDGES),
        bbox_area=_histogram(area_values, BBOX_AREA_EDGES),
        bbox_ratio=_histogram(ratio_values, BBOX_RATIO_EDGES),
        brightness=_histogram(brightness_values, BRIGHTNESS_EDGES),
        n_samples=len(features),
    )


# Защита от log(0) в PSI. Учебное floor-значение 1e-4 для 10 бинов.
_PSI_EPSILON: float = 1e-4


def psi(reference: Sequence[float], current: Sequence[float]) -> float:
    """Population Stability Index — формула (2.11) пояснительной записки.

    PSI = Σ (p_c - p_r) · ln(p_c / p_r) по всем бинам.

    Интерпретация (учебная):
      * PSI < 0.1 — стабильно;
      * 0.1 ≤ PSI < 0.2 — заметный сдвиг, контролировать;
      * PSI ≥ 0.2 — сильный дрейф, действовать.
    """
    if len(reference) != len(current):
        raise ValueError(
            f"PSI length mismatch: reference={len(reference)} current={len(current)}"
        )
    score = 0.0
    for p_ref, p_cur in zip(reference, current):
        p_ref_safe = max(p_ref, _PSI_EPSILON)
        p_cur_safe = max(p_cur, _PSI_EPSILON)
        score += (p_cur_safe - p_ref_safe) * math.log(p_cur_safe / p_ref_safe)
    return round(score, 6)


def csi(reference: Sequence[float], current: Sequence[float]) -> float:
    """CSI — формула идентична PSI, отличается семантика входа.

    PSI применяется к выходному распределению модели (confidence),
    CSI — к распределениям входных характеристик (площадь bbox, его
    aspect ratio, brightness кадра).
    """
    return psi(reference, current)


def compare_drift(
    reference: FeatureHistograms,
    current: FeatureHistograms,
) -> DriftScores:
    """Посчитать 1 × PSI (confidence) + 3 × CSI (bbox area, bbox ratio,
    brightness) между current и reference гистограммами."""
    return DriftScores(
        psi_confidence=psi(reference.confidence, current.confidence),
        csi_bbox_area=csi(reference.bbox_area, current.bbox_area),
        csi_bbox_ratio=csi(reference.bbox_ratio, current.bbox_ratio),
        csi_brightness=csi(reference.brightness, current.brightness),
    )


def _histogram(values: list[float], edges: Sequence[float]) -> tuple[float, ...]:
    """Нормализованная гистограмма: counts / total в каждом бине."""
    bins = [0] * (len(edges) - 1)
    if not values:
        return tuple(0.0 for _ in bins)

    for v in values:
        idx = _bin_index(v, edges)
        bins[idx] += 1

    total = sum(bins)
    if total == 0:
        return tuple(0.0 for _ in bins)
    return tuple(round(c / total, 6) for c in bins)


def _bin_index(value: float, edges: Sequence[float]) -> int:
    """Положить value в один из N бинов [edges[i], edges[i+1])."""
    n_bins = len(edges) - 1
    if value <= edges[0]:
        return 0
    if value >= edges[-1]:
        return n_bins - 1
    for i in range(n_bins):
        if edges[i] <= value < edges[i + 1]:
            return i
    return n_bins - 1  # pragma: no cover — должны были вернуться выше


def _zero_hist() -> tuple[float, ...]:
    return tuple(0.0 for _ in range(DRIFT_BINS))
