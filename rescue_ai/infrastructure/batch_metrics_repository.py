"""Postgres repository for summary metrics of the batch ML pipeline.

Each pipeline run of the DAG upserts one row per ``(ds, mission_id)``
tuple into the ``batch_pipeline_metrics`` table. Re-running the same ds
is idempotent: ``ON CONFLICT ... DO UPDATE`` overwrites the row and
refreshes ``updated_at``.
"""

from __future__ import annotations

from dataclasses import dataclass

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
