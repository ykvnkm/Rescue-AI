"""Tests for the batch metrics exporter service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient

from rescue_ai.application.metrics import (
    BATCH_ACCURACY,
    BATCH_DETECTOR_ERRORS,
    BATCH_LAST_RUN_TIMESTAMP,
    BATCH_PRECISION,
    BATCH_RECALL,
    BATCH_ROWS_CORRUPTED,
    DRIFT_CSI,
    DRIFT_PSI,
)
from rescue_ai.interfaces.batch_exporter.run_service import (
    _refresh_drift_gauges,
    _refresh_quality_gauges,
    build_app,
)


@dataclass
class _Cursor:
    rows_by_query: dict[str, tuple[object, ...] | None]
    current_sql: str = ""

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        self.current_sql = sql

    def fetchone(self) -> tuple[object, ...] | None:
        if "batch_pipeline_metrics" in self.current_sql:
            return self.rows_by_query.get("quality")
        return self.rows_by_query.get("drift")


@dataclass
class _Connection:
    cursor_obj: _Cursor

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self.cursor_obj


@dataclass
class _Db:
    rows_by_query: dict[str, tuple[object, ...] | None]
    cursors: list[_Cursor] = field(default_factory=list)

    def connect(self) -> _Connection:
        cursor = _Cursor(self.rows_by_query)
        self.cursors.append(cursor)
        return _Connection(cursor)


def _gauge_value(gauge: Any, *label_values: str) -> float:
    metric = gauge.labels(*label_values) if label_values else gauge
    return float(metric._value.get())


def test_refresh_gauges_from_latest_database_rows() -> None:
    db = _Db(
        {
            "quality": (0.91, 0.82, 0.77, 3, 2, 1_800_000.0),
            "drift": (0.12, 0.2, 0.3, 0.4),
        }
    )

    _refresh_quality_gauges(db)  # type: ignore[arg-type]
    _refresh_drift_gauges(db)  # type: ignore[arg-type]

    assert _gauge_value(BATCH_RECALL) == 0.91
    assert _gauge_value(BATCH_PRECISION) == 0.82
    assert _gauge_value(BATCH_ACCURACY) == 0.77
    assert _gauge_value(BATCH_ROWS_CORRUPTED) == 3.0
    assert _gauge_value(BATCH_DETECTOR_ERRORS) == 2.0
    assert _gauge_value(BATCH_LAST_RUN_TIMESTAMP) == 1_800_000.0
    assert _gauge_value(DRIFT_PSI) == 0.12
    assert _gauge_value(DRIFT_CSI, "bbox_area") == 0.2
    assert _gauge_value(DRIFT_CSI, "bbox_ratio") == 0.3
    assert _gauge_value(DRIFT_CSI, "brightness") == 0.4


def test_build_app_exposes_health_and_metrics() -> None:
    db = _Db({"quality": None, "drift": None})
    app = build_app(db, interval_sec=60.0)  # type: ignore[arg-type]

    with TestClient(app) as client:
        health = client.get("/health")
        metrics = client.get("/metrics")

    assert health.status_code == 200
    assert health.json()["service"] == "rescue-ai-batch-exporter"
    assert metrics.status_code == 200
    assert "text/plain" in metrics.headers["content-type"]
