"""Postgres repository for summary metrics of the batch ML pipeline.

Each pipeline run of the DAG upserts one row per ``(ds, mission_id)``
tuple into the ``batch_pipeline_metrics`` table. Re-running the same ds
is idempotent: ``ON CONFLICT ... DO UPDATE`` overwrites the row and
refreshes ``updated_at``.

Дрейф входных данных (PSI/CSI) хранится в двух вспомогательных
таблицах того же batch-контура (см. infra/postgres/init/040-drift.sql):

  * ``drift_reference`` — эталонные гистограммы одной выбранной миссии.
    В каждый момент времени активен ровно один reference
    (``is_current=TRUE``, контролируется частичным UNIQUE индексом).
    Новый reference создаётся ручным запуском
    ``publish_metrics --as-reference --reference-id <id>``.
  * ``drift_observations`` — одна строка на ``ds`` со значениями
    PSI/CSI относительно current reference. Заполняется в обычном
    daily-режиме того же stage'а ``publish_metrics``.
"""

from __future__ import annotations

from dataclasses import dataclass

from rescue_ai.domain.mission_metrics import (
    BBOX_AREA_EDGES,
    BBOX_RATIO_EDGES,
    BRIGHTNESS_EDGES,
    CONFIDENCE_EDGES,
    DriftScores,
    FeatureHistograms,
)
from rescue_ai.infrastructure.postgres_connection import PostgresDatabase


@dataclass(frozen=True)
class BatchPipelineMetricsRecord:
    """One row of ``batch_pipeline_metrics``."""

    ds: str
    mission_id: str
    rows_total: int
    rows_positive: int
    rows_corrupted: int
    evaluation_count: int
    tp: int
    tn: int
    fp: int
    fn: int
    detector_errors: int
    accuracy: float
    precision: float
    recall: float
    gt_available: bool


@dataclass(frozen=True)
class DriftReferenceRecord:
    """One row of ``drift_reference``. Используется как объект чтения."""

    reference_id: str
    mission_id: str
    ds: str
    confidence_hist: tuple[float, ...]
    bbox_area_hist: tuple[float, ...]
    bbox_ratio_hist: tuple[float, ...]
    brightness_hist: tuple[float, ...]
    n_samples: int
    model_version: str


class PostgresBatchMetricsRepository:
    """Upserts batch pipeline metrics into Postgres."""

    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    def upsert(self, record: BatchPipelineMetricsRecord) -> None:
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO batch_pipeline_metrics (
                        ds, mission_id,
                        rows_total, rows_positive, rows_corrupted,
                        evaluation_count,
                        tp, tn, fp, fn, detector_errors,
                        accuracy, precision, recall, gt_available,
                        updated_at
                    ) VALUES (
                        %s, %s,
                        %s, %s, %s,
                        %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        NOW()
                    )
                    ON CONFLICT (ds, mission_id)
                    DO UPDATE SET
                        rows_total        = EXCLUDED.rows_total,
                        rows_positive     = EXCLUDED.rows_positive,
                        rows_corrupted    = EXCLUDED.rows_corrupted,
                        evaluation_count  = EXCLUDED.evaluation_count,
                        tp                = EXCLUDED.tp,
                        tn                = EXCLUDED.tn,
                        fp                = EXCLUDED.fp,
                        fn                = EXCLUDED.fn,
                        detector_errors   = EXCLUDED.detector_errors,
                        accuracy          = EXCLUDED.accuracy,
                        precision         = EXCLUDED.precision,
                        recall            = EXCLUDED.recall,
                        gt_available      = EXCLUDED.gt_available,
                        updated_at        = NOW()
                    """,
                    (
                        record.ds,
                        record.mission_id,
                        record.rows_total,
                        record.rows_positive,
                        record.rows_corrupted,
                        record.evaluation_count,
                        record.tp,
                        record.tn,
                        record.fp,
                        record.fn,
                        record.detector_errors,
                        record.accuracy,
                        record.precision,
                        record.recall,
                        record.gt_available,
                    ),
                )
            conn.commit()

    # ── Drift API ───────────────────────────────────────────────

    def save_drift_reference(
        self,
        *,
        reference_id: str,
        mission_id: str,
        ds: str,
        histograms: FeatureHistograms,
        model_version: str,
    ) -> None:
        """Атомарно: снять is_current со старых reference и INSERT нового.

        Атомарность достигается одной транзакцией: UPDATE-ом
        снимаем флаг с активного reference, потом INSERT нового с
        is_current=TRUE. Частичный UNIQUE индекс
        ``ux_drift_reference_current`` гарантирует, что в любой момент
        активен ровно один reference.
        """
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE drift_reference SET is_current = FALSE "
                    "WHERE is_current = TRUE"
                )
                cursor.execute(
                    """
                    INSERT INTO drift_reference (
                        reference_id, mission_id, ds,
                        confidence_hist, bbox_area_hist,
                        bbox_ratio_hist, brightness_hist,
                        n_samples,
                        confidence_edges, bbox_area_edges,
                        bbox_ratio_edges, brightness_edges,
                        model_version, is_current, created_at
                    ) VALUES (
                        %s, %s, %s,
                        %s, %s,
                        %s, %s,
                        %s,
                        %s, %s,
                        %s, %s,
                        %s, TRUE, NOW()
                    )
                    ON CONFLICT (reference_id) DO UPDATE SET
                        mission_id        = EXCLUDED.mission_id,
                        ds                = EXCLUDED.ds,
                        confidence_hist   = EXCLUDED.confidence_hist,
                        bbox_area_hist    = EXCLUDED.bbox_area_hist,
                        bbox_ratio_hist   = EXCLUDED.bbox_ratio_hist,
                        brightness_hist   = EXCLUDED.brightness_hist,
                        n_samples         = EXCLUDED.n_samples,
                        confidence_edges  = EXCLUDED.confidence_edges,
                        bbox_area_edges   = EXCLUDED.bbox_area_edges,
                        bbox_ratio_edges  = EXCLUDED.bbox_ratio_edges,
                        brightness_edges  = EXCLUDED.brightness_edges,
                        model_version     = EXCLUDED.model_version,
                        is_current        = TRUE,
                        created_at        = NOW()
                    """,
                    (
                        reference_id,
                        mission_id,
                        ds,
                        list(histograms.confidence),
                        list(histograms.bbox_area),
                        list(histograms.bbox_ratio),
                        list(histograms.brightness),
                        histograms.n_samples,
                        list(CONFIDENCE_EDGES),
                        list(BBOX_AREA_EDGES),
                        list(BBOX_RATIO_EDGES),
                        list(BRIGHTNESS_EDGES),
                        model_version,
                    ),
                )
            conn.commit()

    def save_drift_observation(
        self,
        *,
        ds: str,
        reference_id: str,
        scores: DriftScores,
        n_samples: int,
        n_missions: int,
    ) -> None:
        """Upsert одного наблюдения дрейфа по PK (ds)."""
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO drift_observations (
                        ds, reference_id,
                        psi_confidence, csi_bbox_area,
                        csi_bbox_ratio, csi_brightness,
                        drift_flag, n_samples, n_missions, updated_at
                    ) VALUES (
                        %s, %s,
                        %s, %s,
                        %s, %s,
                        %s, %s, %s, NOW()
                    )
                    ON CONFLICT (ds) DO UPDATE SET
                        reference_id   = EXCLUDED.reference_id,
                        psi_confidence = EXCLUDED.psi_confidence,
                        csi_bbox_area  = EXCLUDED.csi_bbox_area,
                        csi_bbox_ratio = EXCLUDED.csi_bbox_ratio,
                        csi_brightness = EXCLUDED.csi_brightness,
                        drift_flag     = EXCLUDED.drift_flag,
                        n_samples      = EXCLUDED.n_samples,
                        n_missions     = EXCLUDED.n_missions,
                        updated_at     = NOW()
                    """,
                    (
                        ds,
                        reference_id,
                        scores.psi_confidence,
                        scores.csi_bbox_area,
                        scores.csi_bbox_ratio,
                        scores.csi_brightness,
                        scores.drift_flag(),
                        n_samples,
                        n_missions,
                    ),
                )
            conn.commit()

    def load_current_drift_reference(self) -> DriftReferenceRecord | None:
        """Вернуть активный reference (is_current=TRUE) либо None."""
        with self._db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        reference_id, mission_id, ds,
                        confidence_hist, bbox_area_hist,
                        bbox_ratio_hist, brightness_hist,
                        n_samples, model_version
                    FROM drift_reference
                    WHERE is_current = TRUE
                    LIMIT 1
                    """
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return DriftReferenceRecord(
            reference_id=str(row[0]),
            mission_id=str(row[1]),
            ds=str(row[2]),
            confidence_hist=tuple(float(v) for v in row[3]),
            bbox_area_hist=tuple(float(v) for v in row[4]),
            bbox_ratio_hist=tuple(float(v) for v in row[5]),
            brightness_hist=tuple(float(v) for v in row[6]),
            n_samples=int(row[7]),
            model_version=str(row[8]),
        )
