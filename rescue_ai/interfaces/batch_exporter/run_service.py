"""Entry point for the rescue-ai-batch-exporter pod.

Назначение микросервиса — публиковать в Prometheus агрегированные
показатели качества модели, полученные на ежедневном batch-DAG
(см. раздел 2.7 пояснительной записки). Сервис ничего не считает сам:
он периодически читает таблицу ``batch_pipeline_metrics`` и обновляет
соответствующие Prometheus-gauge'и.

Поведение:

* подключается к Postgres по ``DB_DSN``;
* раз в ``BATCH_EXPORTER_SCRAPE_INTERVAL_SEC`` (по умолчанию 300 секунд)
  выбирает последнюю по ``updated_at`` запись и проставляет gauge'и
  ``rescue_ai_batch_recall``, ``rescue_ai_batch_precision``,
  ``rescue_ai_batch_accuracy``, ``rescue_ai_batch_rows_corrupted``,
  ``rescue_ai_batch_detector_errors``,
  ``rescue_ai_batch_last_run_timestamp_seconds``;
* публикует эндпоинт ``/metrics`` на порту ``BATCH_EXPORTER_PORT``
  (по умолчанию 8003), который опрашивает Prometheus.

Сервис умышленно сделан тонким: вся бизнес-логика подсчёта качества
живёт в Airflow DAG. Здесь только мост между таблицей и Prometheus.
"""

from __future__ import annotations

import importlib
import logging
import os
import time
from threading import Event, Thread

import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response

from rescue_ai.application.metrics import (
    BATCH_ACCURACY,
    BATCH_DETECTOR_ERRORS,
    BATCH_LAST_RUN_TIMESTAMP,
    BATCH_PRECISION,
    BATCH_RECALL,
    BATCH_ROWS_CORRUPTED,
    render_latest,
)
from rescue_ai.infrastructure.postgres_connection import PostgresDatabase

logger = logging.getLogger(__name__)

_SCRAPE_QUERY = """
SELECT
    recall,
    precision,
    accuracy,
    rows_corrupted,
    detector_errors,
    EXTRACT(EPOCH FROM updated_at)::DOUBLE PRECISION AS updated_at_epoch
FROM batch_pipeline_metrics
ORDER BY updated_at DESC
LIMIT 1
"""


def _postgres_error_types() -> tuple[type[BaseException], ...]:
    """Return psycopg exception types without making psycopg a hard import."""
    try:
        psycopg = importlib.import_module("psycopg")
    except ImportError:  # pragma: no cover - optional dependency guard
        return (OSError, RuntimeError)
    postgres_error = getattr(psycopg, "Error", None)
    if isinstance(postgres_error, type) and issubclass(postgres_error, BaseException):
        return (postgres_error, OSError, RuntimeError)
    return (OSError, RuntimeError)


def _refresh_gauges(db: PostgresDatabase) -> None:
    """Прочитать последнюю запись batch_pipeline_metrics и обновить gauge'и."""
    try:
        with db.connect() as conn, conn.cursor() as cursor:
            cursor.execute(_SCRAPE_QUERY)
            row = cursor.fetchone()
    except _postgres_error_types():  # pragma: no cover - Postgres недоступен
        logger.exception("batch-exporter: failed to read batch_pipeline_metrics")
        return
    if row is None:
        logger.info("batch-exporter: batch_pipeline_metrics is empty, skipping")
        return
    recall, precision, accuracy, rows_corrupted, detector_errors, updated_at = row
    BATCH_RECALL.set(float(recall))
    BATCH_PRECISION.set(float(precision))
    BATCH_ACCURACY.set(float(accuracy))
    BATCH_ROWS_CORRUPTED.set(float(rows_corrupted))
    BATCH_DETECTOR_ERRORS.set(float(detector_errors))
    BATCH_LAST_RUN_TIMESTAMP.set(float(updated_at))
    logger.info(
        "batch-exporter: refreshed gauges (recall=%.4f precision=%.4f accuracy=%.4f)",
        float(recall),
        float(precision),
        float(accuracy),
    )


def _scrape_loop(db: PostgresDatabase, interval_sec: float, stop: Event) -> None:
    """Фоновый цикл периодического обновления gauge'ов."""
    while not stop.is_set():
        _refresh_gauges(db)
        # Event.wait() корректно прерывается при stop.set().
        stop.wait(timeout=interval_sec)


def build_app(db: PostgresDatabase, interval_sec: float) -> FastAPI:
    """Собрать FastAPI-приложение exporter'а и запустить фоновый цикл."""
    app = FastAPI(
        title="Rescue-AI Batch Metrics Exporter",
        description=(
            "Публикует агрегированные показатели качества модели "
            "из таблицы batch_pipeline_metrics в формате Prometheus."
        ),
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
    )
    stop_event = Event()
    worker = Thread(
        target=_scrape_loop,
        args=(db, interval_sec, stop_event),
        name="batch-exporter-scrape-loop",
        daemon=True,
    )

    @app.on_event("startup")
    def _start_worker() -> None:  # pragma: no cover - lifecycle wiring
        worker.start()

    @app.on_event("shutdown")
    def _stop_worker() -> None:  # pragma: no cover - lifecycle wiring
        stop_event.set()
        worker.join(timeout=5.0)

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        payload, content_type = render_latest()
        return Response(content=payload, media_type=content_type)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, object]:
        last_refresh_epoch = float(BATCH_LAST_RUN_TIMESTAMP._value.get())
        return {
            "service": "rescue-ai-batch-exporter",
            "last_refresh_epoch": last_refresh_epoch,
        }

    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("APP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    dsn = os.environ.get("DB_DSN", "")
    if not dsn:
        raise RuntimeError("batch-exporter requires DB_DSN to be set")
    db = PostgresDatabase(dsn=dsn)
    interval_sec = float(os.environ.get("BATCH_EXPORTER_SCRAPE_INTERVAL_SEC", "300"))
    host = os.environ.get("BATCH_EXPORTER_HOST", "0.0.0.0")
    port = int(os.environ.get("BATCH_EXPORTER_PORT", "8003"))
    app = build_app(db, interval_sec=interval_sec)
    # Прогреваем gauge'и до старта uvicorn, чтобы первый scrape
    # уже отдавал актуальные значения.
    _refresh_gauges(db)
    # Текущее время в качестве startup-маркера полезно для дашборда:
    BATCH_LAST_RUN_TIMESTAMP.set(time.time())
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
