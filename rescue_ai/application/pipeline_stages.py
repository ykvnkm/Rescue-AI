"""Pipeline stage functions for the unified ML pipeline.

Each stage is idempotent: it checks for an existing artifact and skips
unless ``force=True``.  Stages communicate through ``StageStorage``
(local files or S3 JSON objects) and share a ``PipelinePaths`` key
builder so that every artifact URI is deterministic.

Stage flow:
    data  →  train  →  validate  →  inference
"""

from __future__ import annotations

import hashlib
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
        """Serialise *payload* as JSON and write it to *key*."""

    def uri(self, key: str) -> str:
        """Return the canonical URI for *key*."""


# ── Deterministic key builder ───────────────────────────────────


@dataclass(frozen=True)
class PipelinePaths:
    """Builds deterministic artifact keys for every stage."""

    prefix: str
    mission_id: str
    ds: str
    model_version: str
    code_version: str

    @property
    def base(self) -> str:
        """Return the common key prefix for all artifacts in this run."""
        prefix = self.prefix.strip("/")
        root = f"ml_pipeline/mission={self.mission_id}/ds={self.ds}"
        return f"{prefix}/{root}" if prefix else root

    @property
    def data_key(self) -> str:
        """Return the storage key for the dataset manifest."""
        return f"{self.base}/dataset.json"

    @property
    def model_key(self) -> str:
        """Return the storage key for the trained model card."""
        mv = _slug(self.model_version)
        cv = _slug(self.code_version)
        return f"{self.base}/model_{mv}_{cv}.json"

    @property
    def validation_key(self) -> str:
        """Return the storage key for the validation report."""
        mv = _slug(self.model_version)
        cv = _slug(self.code_version)
        return f"{self.base}/validation_{mv}_{cv}.json"

    @property
    def inference_key(self) -> str:
        """Return the storage key for the inference results."""
        mv = _slug(self.model_version)
        cv = _slug(self.code_version)
        return f"{self.base}/inference_{mv}_{cv}.json"


@dataclass
class ValidationCounts:
    """Accumulated confusion-matrix counters for validation."""

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
            raise RuntimeError("validation has no processable samples")
        return round((self.tp + self.tn) / self.total, 4)


# ── Stage 1: data preparation ──────────────────────────────────


def run_data_stage(
    store: StageStorage,
    paths: PipelinePaths,
    *,
    force: bool = False,
    mission_loader=None,
) -> dict[str, object]:
    """Build dataset manifest from real mission frames."""
    if store.exists(paths.data_key) and not force:
        return _skip("data", store.uri(paths.data_key))

    if mission_loader is None:
        raise RuntimeError("mission_loader is required for data stage")

    mission_input = mission_loader()
    valid_frames = [frame for frame in mission_input.frames if not frame.is_corrupted]
    corrupted_count = len(mission_input.frames) - len(valid_frames)
    if not valid_frames:
        raise RuntimeError("mission has no valid frames")

    split_index = max(1, int(len(valid_frames) * 0.8))
    split_index = min(split_index, len(valid_frames))
    train_frames = valid_frames[:split_index]
    val_frames = valid_frames[split_index:] or valid_frames[-1:]
    positives = sum(1 for frame in valid_frames if frame.gt_person_present)

    payload: dict[str, object] = {
        "stage": "data",
        "created_at": _now_iso(),
        "mission_id": paths.mission_id,
        "ds": paths.ds,
        "source_uri": mission_input.source_uri,
        "gt_available": mission_input.gt_available,
        "rows_total": len(valid_frames),
        "rows_positive": positives,
        "rows_corrupted": corrupted_count,
        "train_count": len(train_frames),
        "val_count": len(val_frames),
        "train_manifest": [
            {
                "image_uri": frame.image_uri,
                "gt_person_present": bool(frame.gt_person_present),
            }
            for frame in train_frames
        ],
        "val_manifest": [
            {
                "image_uri": frame.image_uri,
                "gt_person_present": bool(frame.gt_person_present),
            }
            for frame in val_frames
        ],
        "feature_hash": hashlib.sha256(
            f"{paths.mission_id}:{paths.ds}".encode()
        ).hexdigest()[:16],
    }
    store.write_json(paths.data_key, payload)
    return _done("data", store.uri(paths.data_key))


# ── Stage 2: model registration (train) ────────────────────────


def run_train_stage(
    store: StageStorage,
    paths: PipelinePaths,
    *,
    force: bool = False,
    model_probe=None,
) -> dict[str, object]:
    """Prepare runtime model and write model card."""
    if not store.exists(paths.data_key):
        raise RuntimeError(f"dataset is missing: {store.uri(paths.data_key)}")
    if store.exists(paths.model_key) and not force:
        return _skip("train", store.uri(paths.model_key))

    dataset = store.read_json(paths.data_key)
    rows_total = _as_int(dataset.get("rows_total"), field_name="rows_total")
    rows_positive = _as_int(dataset.get("rows_positive"), field_name="rows_positive")
    if rows_total <= 0:
        raise RuntimeError("dataset has zero rows")
    if model_probe is None:
        raise RuntimeError("model_probe is required for train stage")

    model_meta = model_probe()
    if not isinstance(model_meta, dict):
        raise RuntimeError("model_probe must return a dict")

    class_ratio = rows_positive / rows_total
    payload: dict[str, object] = {
        "stage": "train",
        "created_at": _now_iso(),
        "mission_id": paths.mission_id,
        "ds": paths.ds,
        "model_version": paths.model_version,
        "code_version": paths.code_version,
        "dataset_uri": store.uri(paths.data_key),
        "class_ratio": round(class_ratio, 6),
        "train_count": _as_int(dataset.get("train_count"), field_name="train_count"),
        "val_count": _as_int(dataset.get("val_count"), field_name="val_count"),
        "model_runtime": str(model_meta.get("runtime", "unknown")),
        "model_url": str(model_meta.get("model_url", "")),
        "model_ready": bool(model_meta.get("model_ready", True)),
        "checkpoint_hash": hashlib.sha256(
            f"{paths.model_version}:{paths.code_version}:{dataset}".encode()
        ).hexdigest()[:16],
    }
    store.write_json(paths.model_key, payload)
    return _done("train", store.uri(paths.model_key))


# ── Stage 3: validation (quality gate) ─────────────────────────


def run_validate_stage(
    store: StageStorage,
    paths: PipelinePaths,
    *,
    force: bool = False,
    min_accuracy: float = 0.75,
    detector_predict=None,
) -> dict[str, object]:
    """Run validation on val split using detector predictions."""
    if store.exists(paths.validation_key) and not force:
        return _skip("validate", store.uri(paths.validation_key))
    _require_validation_prerequisites(store, paths)
    if detector_predict is None:
        raise RuntimeError("detector_predict is required for validate stage")

    dataset = store.read_json(paths.data_key)
    _ensure_dataset_has_rows(dataset)
    gt_available = bool(dataset.get("gt_available", True))
    val_manifest = _parse_val_manifest(dataset)
    counts = _evaluate_validation_manifest(
        val_manifest=val_manifest,
        detector_predict=detector_predict,
    )
    accuracy = counts.accuracy if counts.total > 0 else 1.0

    # Without ground truth labels accuracy is not meaningful — skip the gate.
    passed = True if not gt_available else accuracy >= min_accuracy

    payload: dict[str, object] = {
        "stage": "validate",
        "created_at": _now_iso(),
        "mission_id": paths.mission_id,
        "ds": paths.ds,
        "dataset_uri": store.uri(paths.data_key),
        "model_uri": store.uri(paths.model_key),
        "samples_total": counts.total,
        "tp": counts.tp,
        "tn": counts.tn,
        "fp": counts.fp,
        "fn": counts.fn,
        "detector_errors": counts.detector_errors,
        "accuracy": accuracy,
        "min_accuracy": min_accuracy,
        "gt_available": gt_available,
        "passed": passed,
    }
    store.write_json(paths.validation_key, payload)

    if not passed:
        raise RuntimeError(
            f"validation failed: accuracy={accuracy} < threshold={min_accuracy}"
        )
    return _done("validate", store.uri(paths.validation_key))


# ── Stage 4: batch inference ────────────────────────────────────


def run_inference_stage(
    store: StageStorage,
    paths: PipelinePaths,
    *,
    force: bool = False,
    runner_factory=None,
) -> dict[str, object]:
    """Run batch inference using MissionBatchRunner, gate on validation.

    Parameters
    ----------
    runner_factory:
        Callable ``() -> (MissionBatchRunner, BatchRunRequest)`` injected by
        the CLI layer so this module stays free of infrastructure imports.
    """
    if not store.exists(paths.validation_key):
        raise RuntimeError(
            f"validation artifact is missing: {store.uri(paths.validation_key)}"
        )
    if not store.read_json(paths.validation_key).get("passed"):
        raise RuntimeError(
            "validation did not pass — inference blocked "
            f"(accuracy={store.read_json(paths.validation_key).get('accuracy')})"
        )

    if store.exists(paths.inference_key) and not force:
        return _skip("inference", store.uri(paths.inference_key))

    if runner_factory is None:
        raise RuntimeError("runner_factory is required for inference stage")

    runner, request = runner_factory()
    run_key, status, report_uri, debug_uri, db_unavailable = _run_inference_with_guard(
        runner=runner,
        request=request,
    )

    payload: dict[str, object] = {
        "stage": "inference",
        "created_at": _now_iso(),
        "mission_id": paths.mission_id,
        "ds": paths.ds,
        "model_version": paths.model_version,
        "code_version": paths.code_version,
        "run_key": run_key,
        "status": status,
        "report_uri": report_uri,
        "debug_uri": debug_uri,
        "db_unavailable": db_unavailable,
    }
    store.write_json(paths.inference_key, payload)
    return _done("inference", store.uri(paths.inference_key))


# ── Helpers ─────────────────────────────────────────────────────


def _skip(stage: str, uri: str) -> dict[str, object]:
    return {"stage": stage, "status": "idempotent_skip", "output_uri": uri}


def _done(stage: str, uri: str) -> dict[str, object]:
    return {"stage": stage, "status": "completed", "output_uri": uri}


def _run_inference_with_guard(
    *,
    runner,
    request,
) -> tuple[str, str, str | None, str | None, bool]:
    try:
        result = runner.run(request)
        return (
            result.run_key,
            result.status,
            result.report_uri,
            result.debug_uri,
            False,
        )
    except (ConnectionError, TimeoutError, OSError, RuntimeError) as exc:
        if _is_db_unavailable_error(exc):
            print(
                f"[WARN] DB unavailable during inference ({exc});"
                " writing placeholder result"
            )
            return (request.run_key, "completed_no_db", None, None, True)
        raise


def _is_db_unavailable_error(exc: Exception) -> bool:
    exc_text = str(exc).lower()
    return any(
        keyword in exc_text for keyword in ("timeout", "connection", "operational")
    )


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value.strip().lower()).strip(
        "_"
    )


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


def _require_validation_prerequisites(
    store: StageStorage,
    paths: PipelinePaths,
) -> None:
    if not store.exists(paths.data_key):
        raise RuntimeError(f"dataset is missing: {store.uri(paths.data_key)}")
    if not store.exists(paths.model_key):
        raise RuntimeError(f"model artifact is missing: {store.uri(paths.model_key)}")


def _ensure_dataset_has_rows(dataset: dict[str, object]) -> None:
    rows_total = _as_int(dataset.get("rows_total"), field_name="rows_total")
    if rows_total <= 0:
        raise RuntimeError("dataset has zero rows")


def _parse_val_manifest(dataset: dict[str, object]) -> list[dict[str, object]]:
    val_manifest_raw = dataset.get("val_manifest")
    if not isinstance(val_manifest_raw, list) or not val_manifest_raw:
        raise RuntimeError("val_manifest is missing in dataset artifact")
    if not all(isinstance(item, dict) for item in val_manifest_raw):
        raise RuntimeError("val_manifest item must be an object")
    return [item for item in val_manifest_raw if isinstance(item, dict)]


def _evaluate_validation_manifest(
    *,
    val_manifest: list[dict[str, object]],
    detector_predict,
) -> ValidationCounts:
    counts = ValidationCounts()
    for item in val_manifest:
        image_uri = str(item.get("image_uri", ""))
        if not image_uri:
            raise RuntimeError("val_manifest item has empty image_uri")
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


def print_result(result: dict[str, object]) -> None:
    """Print stage result as JSON to stdout (used by CLI layer)."""
    print(json.dumps(result, ensure_ascii=False))
