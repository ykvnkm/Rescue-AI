"""Prometheus-метрики Rescue-AI.

Состав метрик соответствует разделу 2.7 пояснительной записки:

- counter — события, число которых только возрастает в жизненном цикле
  процесса (детекции, созданные сигналы, технические сбои инференса);
- histogram — длительности обработки (запуск модели, обновление
  навигационного трека, обработка HTTP-запроса);
- gauge — мгновенные состояния (доступность бортового модуля, размер
  буфера кадров, последние агрегированные показатели batch-прогона).

Метрики живут в едином реестре ``REGISTRY`` (стандартный реестр
``prometheus_client``). Эндпоинт ``/metrics`` сериализует их в формате
text-exposition. Имена меток выбраны короткими; кардинальность ограничена
заранее известными значениями (например, ``mission_mode`` принимает два
значения — ``manual`` и ``auto``).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter
from typing import Final

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------

DETECTIONS_TOTAL: Final = Counter(
    "rescue_ai_detections_total",
    "Общее число детекций модели обнаружения.",
    labelnames=("model", "label"),
)

ALERTS_CREATED_TOTAL: Final = Counter(
    "rescue_ai_alerts_created_total",
    "Число созданных сигналов с разбивкой по режиму миссии.",
    labelnames=("mission_mode",),
)

DETECTOR_ERRORS_TOTAL: Final = Counter(
    "rescue_ai_detector_errors_total",
    "Технические сбои запуска модели обнаружения.",
    labelnames=("model",),
)


# ---------------------------------------------------------------------------
# Histograms
# ---------------------------------------------------------------------------

# Buckets подобраны под потоковую обработку: целевая p95 — 0.2 с
# (см. раздел 2.7 и порог Alertmanager).
_LATENCY_BUCKETS: Final = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.075,
    0.10,
    0.15,
    0.20,
    0.30,
    0.50,
    1.0,
    2.0,
)

INFERENCE_DURATION_SECONDS: Final = Histogram(
    "rescue_ai_inference_duration_seconds",
    "Распределение времени запуска модели обнаружения по одному кадру.",
    labelnames=("model",),
    buckets=_LATENCY_BUCKETS,
)

NAVIGATION_UPDATE_DURATION_SECONDS: Final = Histogram(
    "rescue_ai_navigation_update_duration_seconds",
    "Распределение времени обновления навигационного трека по кадру.",
    buckets=_LATENCY_BUCKETS,
)

HTTP_REQUEST_DURATION_SECONDS: Final = Histogram(
    "http_request_duration_seconds",
    "Распределение времени обработки HTTP-запроса.",
    labelnames=("method", "route", "status"),
    buckets=_LATENCY_BUCKETS,
)


# ---------------------------------------------------------------------------
# Gauges
# ---------------------------------------------------------------------------

RPI_CONNECTIVITY_UP: Final = Gauge(
    "rescue_ai_rpi_connectivity_up",
    "Доступность бортового модуля Raspberry Pi (1 — доступен, 0 — нет).",
)

CURRENT_BUFFER_SIZE: Final = Gauge(
    "rescue_ai_current_buffer_size",
    "Текущий размер локального буфера кадров миссии.",
)

# Последние агрегированные batch-метрики качества модели.
# Заполняются отдельным процессом (batch-exporter) по таблице
# ``batch_pipeline_metrics``.
BATCH_RECALL: Final = Gauge(
    "rescue_ai_batch_recall",
    "Последняя полнота обнаружения, рассчитанная batch-DAG.",
)
BATCH_PRECISION: Final = Gauge(
    "rescue_ai_batch_precision",
    "Последняя точность положительных предсказаний, рассчитанная batch-DAG.",
)
BATCH_ACCURACY: Final = Gauge(
    "rescue_ai_batch_accuracy",
    "Последняя общая точность, рассчитанная batch-DAG.",
)
BATCH_ROWS_CORRUPTED: Final = Gauge(
    "rescue_ai_batch_rows_corrupted",
    "Число повреждённых кадров в последнем batch-прогоне.",
)
BATCH_DETECTOR_ERRORS: Final = Gauge(
    "rescue_ai_batch_detector_errors",
    "Число технических отказов инференса в последнем batch-прогоне.",
)
BATCH_LAST_RUN_TIMESTAMP: Final = Gauge(
    "rescue_ai_batch_last_run_timestamp_seconds",
    "Unix-время последнего опубликованного batch-прогона.",
)

# Дрейф входных распределений: PSI по уверенности модели и CSI по
# отдельным характеристикам данных (площадь рамки, отношение сторон,
# яркость кадра).
DRIFT_PSI: Final = Gauge(
    "rescue_ai_drift_psi_score",
    "PSI распределения уверенности модели по сравнению с baseline.",
)
DRIFT_CSI: Final = Gauge(
    "rescue_ai_drift_csi",
    "CSI отдельной характеристики данных.",
    labelnames=("feature",),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def observe_duration(histogram: Histogram, **labels: str) -> Iterator[None]:
    """Засечь длительность блока кода и записать значение в histogram.

    Использование::

        with observe_duration(INFERENCE_DURATION_SECONDS, model="yolov8n"):
            detector.detect(frame)
    """
    start = perf_counter()
    try:
        yield
    finally:
        elapsed = perf_counter() - start
        if labels:
            histogram.labels(**labels).observe(elapsed)
        else:
            histogram.observe(elapsed)


def render_latest() -> tuple[bytes, str]:
    """Сериализовать все метрики в формат text-exposition."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


__all__ = [
    "REGISTRY",
    "CONTENT_TYPE_LATEST",
    "DETECTIONS_TOTAL",
    "ALERTS_CREATED_TOTAL",
    "DETECTOR_ERRORS_TOTAL",
    "INFERENCE_DURATION_SECONDS",
    "NAVIGATION_UPDATE_DURATION_SECONDS",
    "HTTP_REQUEST_DURATION_SECONDS",
    "RPI_CONNECTIVITY_UP",
    "CURRENT_BUFFER_SIZE",
    "BATCH_RECALL",
    "BATCH_PRECISION",
    "BATCH_ACCURACY",
    "BATCH_ROWS_CORRUPTED",
    "BATCH_DETECTOR_ERRORS",
    "BATCH_LAST_RUN_TIMESTAMP",
    "DRIFT_PSI",
    "DRIFT_CSI",
    "observe_duration",
    "render_latest",
]
