"""Postgres repository for summary metrics of the batch ML pipeline.

Each pipeline run of the DAG writes exactly one row per
``(ds, mission_id, model_version, code_version)`` tuple into the
``batch_pipeline_metrics`` table. Re-runs upsert the row so that
``updated_at`` always reflects the latest successful execution — this is
what makes idempotency observable from a BI dashboard (or from a
``SELECT ... WHERE ds = CURRENT_DATE``).
"""

from __future__ import annotations

from dataclasses import dataclass

from rescue_ai.infrastructure.postgres_connection import PostgresDatabase


@dataclass(frozen=True)
class BatchPipelineMetricsRecord:
    """One row of ``batch_pipeline_metrics``."""

    ds: str
    mission_id: str
    model_version: str
    code_version: str
    rows_total: int
    rows_positive: int
    rows_corrupted: int
    train_count: int
    val_count: int
    samples_total: int
    tp: int
    tn: int
    fp: int
    fn: int
    detector_errors: int
    accuracy: float
    recall: float
    gt_available: bool
    validate_passed: bool


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
                        ds, mission_id, model_version, code_version,
                        rows_total, rows_positive, rows_corrupted,
                        train_count, val_count,
                        samples_total, tp, tn, fp, fn, detector_errors,
                        accuracy, recall, gt_available, validate_passed,
                        updated_at
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        NOW()
                    )
                    ON CONFLICT (ds, mission_id, model_version, code_version)
                    DO UPDATE SET
                        rows_total        = EXCLUDED.rows_total,
                        rows_positive     = EXCLUDED.rows_positive,
                        rows_corrupted    = EXCLUDED.rows_corrupted,
                        train_count       = EXCLUDED.train_count,
                        val_count         = EXCLUDED.val_count,
                        samples_total     = EXCLUDED.samples_total,
                        tp                = EXCLUDED.tp,
                        tn                = EXCLUDED.tn,
                        fp                = EXCLUDED.fp,
                        fn                = EXCLUDED.fn,
                        detector_errors   = EXCLUDED.detector_errors,
                        accuracy          = EXCLUDED.accuracy,
                        recall            = EXCLUDED.recall,
                        gt_available      = EXCLUDED.gt_available,
                        validate_passed   = EXCLUDED.validate_passed,
                        updated_at        = NOW()
                    """,
                    (
                        record.ds,
                        record.mission_id,
                        record.model_version,
                        record.code_version,
                        record.rows_total,
                        record.rows_positive,
                        record.rows_corrupted,
                        record.train_count,
                        record.val_count,
                        record.samples_total,
                        record.tp,
                        record.tn,
                        record.fp,
                        record.fn,
                        record.detector_errors,
                        record.accuracy,
                        record.recall,
                        record.gt_available,
                        record.validate_passed,
                    ),
                )
            conn.commit()
