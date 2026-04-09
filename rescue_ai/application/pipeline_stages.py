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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

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
) -> dict[str, object]:
    """Run the deployed detector over the dataset manifest.

    Loads the dataset built by ``prepare_dataset``, runs the predictor
    over every frame to build a confusion matrix, and writes the result
    to S3 (overwrites any prior evaluation for this
    ``(ds, mission)``).
    """
    if not store.exists(paths.dataset_key):
        raise RuntimeError(f"dataset is missing: {store.uri(paths.dataset_key)}")
    if detector_predict is None:
        raise RuntimeError("detector_predict is required for evaluate_model stage")

    dataset = store.read_json(paths.dataset_key)
    _ensure_dataset_has_rows(dataset)
    gt_available = bool(dataset.get("gt_available", True))
    evaluation_manifest = _parse_evaluation_manifest(dataset)
    counts = _evaluate(
        evaluation_manifest=evaluation_manifest,
        detector_predict=detector_predict,
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


def run_publish_metrics_stage(
    store: StageStorage,
    paths: PipelinePaths,
    *,
    metrics_writer,
    record_factory,
) -> dict[str, object]:
    """Read stage artifacts and upsert one summary row into Postgres.

    This stage is the only place where the batch pipeline writes into the
    application Postgres. It is always safe to re-run — the repository
    uses ``ON CONFLICT (ds, mission_id) DO UPDATE``, so re-running for
    the same ``ds`` overwrites the row in place, while a backfill across
    a date range inserts one row per ``(ds, mission)``.
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
    return {
        "stage": "publish_metrics",
        "status": "completed",
        "ds": paths.ds,
        "mission_id": paths.mission_id,
        "metrics": _metric_summary(evaluation)
        | {
            "rows_total": dataset.get("rows_total"),
            "rows_positive": dataset.get("rows_positive"),
            "rows_corrupted": dataset.get("rows_corrupted"),
            "evaluation_count": dataset.get("evaluation_count"),
        },
    }


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
) -> ValidationCounts:
    counts = ValidationCounts()
    for item in evaluation_manifest:
        image_uri = str(item.get("image_uri", ""))
        if not image_uri:
            raise RuntimeError("evaluation_manifest item has empty image_uri")
        gt_present = _as_bool(
            item.get("gt_person_present"), field_name="gt_person_present"
        )
        try:
            detected = bool(detector_predict(image_uri))
        except (RuntimeError, ValueError, OSError) as error:
            counts.detector_errors += 1
            raise RuntimeError(f"detector failed on {image_uri}: {error}") from error
        counts.add(detected=detected, gt_present=gt_present)
    return counts


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
