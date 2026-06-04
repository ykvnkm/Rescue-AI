"""Stage functions for the batch ML pipeline.

Three stages invoked in order by the daily DAG:

* ``prepare_dataset``  — build a dataset manifest from a mission's frames.
* ``evaluate_model``   — run the detector over the manifest, write metrics.
* ``publish_metrics``  — upsert one summary row per mission into Postgres.

Rerun semantics: S3 ``put_object`` overwrites artifacts in place, and
``publish_metrics`` upserts on ``(ds, mission_id)``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Callable, Optional, Protocol, Sequence

from rescue_ai.domain.mission_metrics import (
    DriftScores,
    FeatureHistograms,
    FrameFeatures,
    build_histograms,
    compare_drift,
)

# ── Storage protocol ────────────────────────────────────────────


class StageStorage(Protocol):
    """Minimal JSON-based artifact store used by every stage."""

    def exists(self, key: str) -> bool:
        """Return True if an artifact exists at *key*."""

    def read_json(self, key: str) -> dict[str, object]:
        """Read and return the JSON artifact stored at *key*."""

    def write_json(self, key: str, payload: dict[str, object]) -> None:
        """Serialise *payload* as JSON and write it to *key* (overwrites)."""

    def uri(self, key: str) -> str:
        """Return the canonical URI for *key*."""


# ── Deterministic key builder ───────────────────────────────────


@dataclass(frozen=True)
class PipelinePaths:
    """Builds deterministic artifact keys for one mission/ds."""

    prefix: str
    mission_id: str
    ds: str

    @property
    def base(self) -> str:
        """Return the common key prefix for all artifacts in this run."""
        prefix = self.prefix.strip("/")
        root = f"ml_pipeline/ds={self.ds}/mission={self.mission_id}"
        return f"{prefix}/{root}" if prefix else root

    @property
    def dataset_key(self) -> str:
        """Return the storage key for the dataset manifest."""
        return f"{self.base}/dataset.json"

    @property
    def evaluation_key(self) -> str:
        """Return the storage key for the evaluation report."""
        return f"{self.base}/evaluation.json"


@dataclass
class ValidationCounts:
    """Accumulated confusion-matrix counters for evaluation."""

    tp: int = 0
    tn: int = 0
    fp: int = 0
    fn: int = 0
    detector_errors: int = 0

    def add(self, *, detected: bool, gt_present: bool) -> None:
        if detected and gt_present:
            self.tp += 1
            return
        if detected and not gt_present:
            self.fp += 1
            return
        if not detected and gt_present:
            self.fn += 1
            return
        self.tn += 1

    @property
    def total(self) -> int:
        return self.tp + self.tn + self.fp + self.fn

    @property
    def accuracy(self) -> float:
        if self.total <= 0:
            return 1.0
        return round((self.tp + self.tn) / self.total, 4)

    @property
    def recall(self) -> float:
        positives = self.tp + self.fn
        if positives <= 0:
            return 1.0
        return round(self.tp / positives, 4)

    @property
    def precision(self) -> float:
        predicted_positives = self.tp + self.fp
        if predicted_positives <= 0:
            return 1.0
        return round(self.tp / predicted_positives, 4)


# ── Stage 1: prepare_dataset ────────────────────────────────────


def run_prepare_dataset_stage(
    store: StageStorage,
    paths: PipelinePaths,
    *,
    mission_loader,
) -> dict[str, object]:
    """Build a dataset manifest from a mission's frames and labels.

    Always recomputes and overwrites — that's the whole point of the
    rerun semantics. If the source has new frames or new labels, they
    end up in the new manifest; if nothing changed, the manifest is
    rewritten with the same content (no-op for downstream stages).
    """
    mission_input = mission_loader()
    valid_frames = [frame for frame in mission_input.frames if not frame.is_corrupted]
    corrupted_count = len(mission_input.frames) - len(valid_frames)
    if not valid_frames:
        raise RuntimeError("mission has no valid frames")

    positives = sum(1 for frame in valid_frames if frame.gt_person_present)

    payload: dict[str, object] = {
        "stage": "prepare_dataset",
        "created_at": _now_iso(),
        "mission_id": paths.mission_id,
        "ds": paths.ds,
        "source_uri": mission_input.source_uri,
        "gt_available": mission_input.gt_available,
        "rows_total": len(valid_frames),
        "rows_positive": positives,
        "rows_corrupted": corrupted_count,
        "evaluation_count": len(valid_frames),
        "evaluation_manifest": [
            {
                "image_uri": frame.image_uri,
                "gt_person_present": bool(frame.gt_person_present),
            }
            for frame in valid_frames
        ],
    }
    store.write_json(paths.dataset_key, payload)
    return _done("prepare_dataset", store.uri(paths.dataset_key))


# ── Stage 2: evaluate_model ─────────────────────────────────────


def run_evaluate_model_stage(
    store: StageStorage,
    paths: PipelinePaths,
    *,
    detector_predict,
    frame_loader: Optional[Callable[[str], object]] = None,
) -> dict[str, object]:
    """Run the deployed detector over the dataset manifest.

    Loads the dataset built by ``prepare_dataset``, runs the predictor
    over every frame to build a confusion matrix, and writes the result
    to S3 (overwrites any prior evaluation for this
    ``(ds, mission)``).

    Если передан ``frame_loader`` (image_uri → np.ndarray BGR), на каждом
    кадре также собираются 4 признака для drift (confidence_max,
    bbox_area_norm, bbox_ratio, brightness_mean), которые попадают в
    ``payload["frame_features"]``. Это нужно ML-design-doc §2.7
    drift stage'у. Без frame_loader-а stage работает как раньше —
    drift просто не считается.
    """
    if not store.exists(paths.dataset_key):
        raise RuntimeError(f"dataset is missing: {store.uri(paths.dataset_key)}")
    if detector_predict is None:
        raise RuntimeError("detector_predict is required for evaluate_model stage")

    dataset = store.read_json(paths.dataset_key)
    _ensure_dataset_has_rows(dataset)
    gt_available = bool(dataset.get("gt_available", True))
    evaluation_manifest = _parse_evaluation_manifest(dataset)
    counts, features = _evaluate(
        evaluation_manifest=evaluation_manifest,
        detector_predict=detector_predict,
        frame_loader=frame_loader,
    )

    payload: dict[str, object] = {
        "stage": "evaluate_model",
        "created_at": _now_iso(),
        "mission_id": paths.mission_id,
        "ds": paths.ds,
        "dataset_uri": store.uri(paths.dataset_key),
        "tp": counts.tp,
        "tn": counts.tn,
        "fp": counts.fp,
        "fn": counts.fn,
        "detector_errors": counts.detector_errors,
        "accuracy": counts.accuracy,
        "precision": counts.precision,
        "recall": counts.recall,
        "gt_available": gt_available,
    }
    if features:
        # JSON-friendly: каждая FrameFeatures → dict, None для bbox-полей
        # сохраняется как null (по умолчанию json.dumps).
        payload["frame_features"] = [asdict(f) for f in features]
    store.write_json(paths.evaluation_key, payload)

    if counts.detector_errors > 0:
        raise RuntimeError(
            f"evaluation failed: detector_errors={counts.detector_errors}"
        )
    result = _done("evaluate_model", store.uri(paths.evaluation_key))
    result["metrics"] = _metric_summary(payload)
    return result


# ── Stage 3: publish_metrics ────────────────────────────────────


class BatchMetricsWriter(Protocol):
    """Minimal port for the publish stage (infrastructure-agnostic)."""

    def upsert(self, record: object) -> None:
        """Upsert one summary row into the backing store."""


class DriftStore(Protocol):
    """Минимальный порт для записи drift-наблюдений и эталона.

    Реализуется в ``infrastructure/batch_metrics_repository.py``. Без
    drift-стора publish_metrics просто не считает PSI/CSI (это мягкая
    фича, отсутствие не должно ронять stage).
    """

    def save_drift_reference(
        self,
        *,
        reference_id: str,
        mission_id: str,
        ds: str,
        histograms: FeatureHistograms,
        model_version: str,
    ) -> None:
        """Атомарно: снять is_current со старых reference, INSERT новый."""

    def save_drift_observation(
        self,
        *,
        ds: str,
        reference_id: str,
        scores: DriftScores,
        n_samples: int,
        n_missions: int,
    ) -> None:
        """Upsert drift_observations по PK (ds)."""

    def load_current_drift_reference(self) -> object | None:
        """Вернуть запись reference с is_current=true, либо None."""


def run_publish_metrics_stage(
    store: StageStorage,
    paths: PipelinePaths,
    *,
    metrics_writer,
    record_factory,
    drift_store: DriftStore | None = None,
    drift_evaluation_keys: Sequence[str] | None = None,
    as_reference: bool = False,
    reference_id: str | None = None,
    model_version: str = "unknown",
) -> dict[str, object]:
    """Read stage artifacts and upsert one summary row into Postgres.

    This stage is the only place where the batch pipeline writes into the
    application Postgres. It is always safe to re-run — the repository
    uses ``ON CONFLICT (ds, mission_id) DO UPDATE``, so re-running for
    the same ``ds`` overwrites the row in place, while a backfill across
    a date range inserts one row per ``(ds, mission)``.

    Параметры дрейфа (ML design doc §2.7, формулы (2.11)–(2.12)):
      * ``as_reference=True`` — текущая миссия фиксируется как эталон.
        Гистограммы записываются в таблицу ``drift_reference``,
        ``drift_observations`` для этого ds не обновляется.
      * ``as_reference=False`` (обычный daily-режим) — если задан
        ``drift_evaluation_keys`` со списком evaluation.json ключей всех
        миссий ds, считаются совокупные гистограммы по ним и
        сравниваются с текущим reference. Результат пишется в
        ``drift_observations``.
      * Если ``drift_store=None`` или ``drift_evaluation_keys=None`` —
        drift не считается; legacy-вход с одной миссией работает
        как раньше.
    """
    if not store.exists(paths.dataset_key):
        raise RuntimeError(f"dataset is missing: {store.uri(paths.dataset_key)}")
    if not store.exists(paths.evaluation_key):
        raise RuntimeError(f"evaluation is missing: {store.uri(paths.evaluation_key)}")

    dataset = store.read_json(paths.dataset_key)
    evaluation = store.read_json(paths.evaluation_key)

    record = record_factory(
        paths=paths,
        dataset=dataset,
        evaluation=evaluation,
    )
    metrics_writer.upsert(record)

    drift_summary = _maybe_handle_drift(
        store=store,
        ds=paths.ds,
        mission_id=paths.mission_id,
        evaluation=evaluation,
        drift_store=drift_store,
        drift_evaluation_keys=drift_evaluation_keys,
        as_reference=as_reference,
        reference_id=reference_id,
        model_version=model_version,
    )

    metrics_payload = _metric_summary(evaluation) | {
        "rows_total": dataset.get("rows_total"),
        "rows_positive": dataset.get("rows_positive"),
        "rows_corrupted": dataset.get("rows_corrupted"),
        "evaluation_count": dataset.get("evaluation_count"),
    }
    if drift_summary is not None:
        metrics_payload["drift"] = drift_summary

    return {
        "stage": "publish_metrics",
        "status": "completed",
        "ds": paths.ds,
        "mission_id": paths.mission_id,
        "metrics": metrics_payload,
    }


def _maybe_handle_drift(
    *,
    store: StageStorage,
    ds: str,
    mission_id: str,
    evaluation: dict[str, object],
    drift_store: DriftStore | None,
    drift_evaluation_keys: Sequence[str] | None,
    as_reference: bool,
    reference_id: str | None,
    model_version: str,
) -> dict[str, object] | None:
    """Расчёт PSI/CSI + запись в БД, либо None если drift не включён.

    Логика разделена на 3 ветки:
      * drift_store=None → drift полностью пропущен (legacy);
      * as_reference=True → пишем эталон по features текущей миссии;
      * иначе → агрегируем features со всех миссий ds и сравниваем
        с current reference. Если reference нет — пишем предупреждение
        в summary, но stage не валим.
    """
    if drift_store is None:
        return None

    if as_reference:
        if not reference_id:
            raise RuntimeError(
                "publish_metrics: reference_id required when as_reference=True"
            )
        features = _features_from_evaluation(evaluation)
        if not features:
            raise RuntimeError(
                "publish_metrics --as-reference: evaluation has no frame_features; "
                "rerun evaluate_model with frame_loader configured."
            )
        histograms = build_histograms(features)
        drift_store.save_drift_reference(
            reference_id=reference_id,
            mission_id=mission_id,
            ds=ds,
            histograms=histograms,
            model_version=model_version,
        )
        return {
            "mode": "reference_saved",
            "reference_id": reference_id,
            "n_samples": histograms.n_samples,
        }

    if drift_evaluation_keys is None:
        # daily-режим без явного списка миссий ds — drift не считаем.
        return None

    reference = drift_store.load_current_drift_reference()
    if reference is None:
        return {"mode": "skipped", "reason": "no current drift_reference"}

    all_features: list[FrameFeatures] = []
    n_missions = 0
    for key in drift_evaluation_keys:
        if not store.exists(key):
            continue
        payload = store.read_json(key)
        mission_features = _features_from_evaluation(payload)
        if mission_features:
            all_features.extend(mission_features)
            n_missions += 1

    if not all_features:
        return {"mode": "skipped", "reason": "no frame_features across ds"}

    current = build_histograms(all_features)
    reference_hist = _histograms_from_reference(reference)
    scores = compare_drift(reference_hist, current)
    drift_store.save_drift_observation(
        ds=ds,
        reference_id=getattr(reference, "reference_id"),
        scores=scores,
        n_samples=current.n_samples,
        n_missions=n_missions,
    )
    return {
        "mode": "observation_saved",
        "reference_id": getattr(reference, "reference_id"),
        "psi_confidence": scores.psi_confidence,
        "csi_bbox_area": scores.csi_bbox_area,
        "csi_bbox_ratio": scores.csi_bbox_ratio,
        "csi_brightness": scores.csi_brightness,
        "drift_flag": scores.drift_flag(),
        "n_samples": current.n_samples,
        "n_missions": n_missions,
    }


def _features_from_evaluation(payload: dict[str, object]) -> list[FrameFeatures]:
    """Достать список FrameFeatures из evaluation.json или вернуть []."""
    raw = payload.get("frame_features")
    if not isinstance(raw, list):
        return []
    result: list[FrameFeatures] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        bbox_area_norm = item.get("bbox_area_norm")
        bbox_ratio = item.get("bbox_ratio")
        result.append(
            FrameFeatures(
                confidence_max=float(item.get("confidence_max", 0.0)),
                bbox_area_norm=(
                    None if bbox_area_norm is None else float(bbox_area_norm)
                ),
                bbox_ratio=None if bbox_ratio is None else float(bbox_ratio),
                brightness_mean=float(item.get("brightness_mean", 0.0)),
            )
        )
    return result


def _histograms_from_reference(reference: object) -> FeatureHistograms:
    """Привести запись reference (БД-объект) к FeatureHistograms."""
    return FeatureHistograms(
        confidence=tuple(getattr(reference, "confidence_hist")),
        bbox_area=tuple(getattr(reference, "bbox_area_hist")),
        bbox_ratio=tuple(getattr(reference, "bbox_ratio_hist")),
        brightness=tuple(getattr(reference, "brightness_hist")),
        n_samples=int(getattr(reference, "n_samples")),
    )


# ── Helpers ─────────────────────────────────────────────────────


def _done(stage: str, uri: str) -> dict[str, object]:
    return {"stage": stage, "status": "completed", "output_uri": uri}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _as_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{field_name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as error:
            raise RuntimeError(f"{field_name} must be an integer") from error
    raise RuntimeError(f"{field_name} must be an integer")


def _as_bool(value: object, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes"}:
            return True
        if normalized in {"0", "false", "no"}:
            return False
    raise RuntimeError(f"{field_name} must be boolean")


def _ensure_dataset_has_rows(dataset: dict[str, object]) -> None:
    rows_total = _as_int(dataset.get("rows_total"), field_name="rows_total")
    if rows_total <= 0:
        raise RuntimeError("dataset has zero rows")


def _parse_evaluation_manifest(dataset: dict[str, object]) -> list[dict[str, object]]:
    raw = dataset.get("evaluation_manifest")
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("evaluation_manifest is missing in dataset artifact")
    if not all(isinstance(item, dict) for item in raw):
        raise RuntimeError("evaluation_manifest item must be an object")
    return [item for item in raw if isinstance(item, dict)]


def _evaluate(
    *,
    evaluation_manifest: list[dict[str, object]],
    detector_predict,
    frame_loader: Optional[Callable[[str], object]] = None,
) -> tuple[ValidationCounts, list[FrameFeatures]]:
    """Прогнать детектор по манифесту, собрать counts + features.

    detector_predict: либо ``image_uri → bool`` (упрощённый контракт
    для unit-тестов), либо ``image_uri → list[Detection]`` (рабочий
    контракт CLI: позволяет извлекать confidence/bbox для drift).
    Тип определяем проверкой первого результата — `_to_detected_flag`
    приводит оба варианта к bool.

    frame_loader, если передан, грузит кадр (np.ndarray BGR) — нужен
    только для drift features. Без него ``features`` — пустой список.
    """
    counts = ValidationCounts()
    features_list: list[FrameFeatures] = []
    for item in evaluation_manifest:
        image_uri = str(item.get("image_uri", ""))
        if not image_uri:
            raise RuntimeError("evaluation_manifest item has empty image_uri")
        gt_present = _as_bool(
            item.get("gt_person_present"), field_name="gt_person_present"
        )
        try:
            raw_result = detector_predict(image_uri)
        except (RuntimeError, ValueError, OSError) as error:
            counts.detector_errors += 1
            raise RuntimeError(f"detector failed on {image_uri}: {error}") from error
        detections, detected = _to_detected_flag(raw_result)
        counts.add(detected=detected, gt_present=gt_present)

        # Drift-features собираем только если есть frame_loader И мы
        # умеем извлекать detections (не bool-legacy).
        if frame_loader is not None and detections is not None:
            try:
                frame_bgr = frame_loader(image_uri)
            except (OSError, RuntimeError, ValueError):
                # Если кадр не удалось загрузить — пропускаем drift
                # для него, но evaluation не валим (drift — мягкая фича).
                continue
            features_list.append(_extract_frame_features(frame_bgr, detections))
    return counts, features_list


def _to_detected_flag(
    raw: object,
) -> tuple[list[object] | None, bool]:
    """Привести результат detector_predict к (detections_or_None, bool).

    Возвращает (None, bool) если raw — это уже bool (legacy-test path);
    (list, bool) если raw — это list[Detection] (real path).
    """
    if isinstance(raw, bool):
        return None, raw
    if isinstance(raw, list):
        return raw, len(raw) > 0
    # Пробуем считать «truthy» — для совместимости с прочими формами.
    return None, bool(raw)


def _extract_frame_features(
    frame_bgr: object,
    detections: list[object],
) -> FrameFeatures:
    """Извлечь 4 признака из кадра и (опционально) детекций.

    Импорты cv2/numpy локальны, чтобы pipeline_stages оставался
    лёгким для модулей, которые drift не используют (тесты).
    """
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    frame = np.asarray(frame_bgr)
    if frame.ndim < 2:
        raise ValueError(f"frame must be at least 2D, got shape={frame.shape}")
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    brightness = float(np.mean(gray) / 255.0)

    if not detections:
        return FrameFeatures(
            confidence_max=0.0,
            bbox_area_norm=None,
            bbox_ratio=None,
            brightness_mean=brightness,
        )

    top = max(detections, key=lambda d: float(getattr(d, "score", 0.0)))
    bbox = getattr(top, "bbox", (0.0, 0.0, 0.0, 0.0))
    x1, y1, x2, y2 = (float(v) for v in bbox)
    bw = max(x2 - x1, 1e-6)
    bh = max(y2 - y1, 1e-6)
    return FrameFeatures(
        confidence_max=float(getattr(top, "score", 0.0)),
        bbox_area_norm=float(bw * bh / max(w * h, 1)),
        bbox_ratio=float(bw / bh),
        brightness_mean=brightness,
    )


def _metric_summary(payload: dict[str, object]) -> dict[str, object]:
    return {
        field: payload.get(field)
        for field in (
            "tp",
            "tn",
            "fp",
            "fn",
            "detector_errors",
            "accuracy",
            "precision",
            "recall",
            "gt_available",
        )
    }


def print_result(result: dict[str, object]) -> None:
    """Print stage result to stdout in a log-friendly form."""
    stage = result.get("stage", "?")
    status = result.get("status", "?")
    header_parts = [f"[{stage}] status={status}"]
    if "output_uri" in result:
        header_parts.append(f"uri={result['output_uri']}")
    if "mission_id" in result:
        header_parts.append(f"mission={result['mission_id']}")
    if "ds" in result:
        header_parts.append(f"ds={result['ds']}")
    print(" ".join(header_parts))
    metrics = result.get("metrics")
    if isinstance(metrics, dict) and metrics:
        for key, value in metrics.items():
            print(f"    {key}={value}")
    print(json.dumps(result, ensure_ascii=False, default=str))
