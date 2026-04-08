"""Pipeline stage functions for the unified ML pipeline.

Each stage is idempotent: it checks for an existing artifact and skips
unless ``force=True``.  Stages communicate through ``StageStorage``
(local files or S3 JSON objects) and share a ``PipelinePaths`` key
builder so that every artifact URI is deterministic.

Stage flow:
    data  →  warmup  →  evaluate  →  publish

Semantics:
    * ``data``     — list mission frames and build an evaluation manifest.
    * ``warmup``   — load the deployed detector, run a probe forward pass,
                     write a model card. This is NOT training — the model
                     weights are fixed; the stage only verifies that the
                     runtime can load them before spending compute on the
                     full evaluation.
    * ``evaluate`` — run the deployed detector over the evaluation manifest and
                     record a confusion matrix.
    * ``publish``  — upsert the summary row into Postgres.
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
    def evaluation_key(self) -> str:
        """Return the storage key for the evaluation report."""
        mv = _slug(self.model_version)
        cv = _slug(self.code_version)
        return f"{self.base}/evaluation_{mv}_{cv}.json"


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


# ── Stage 1: data preparation ──────────────────────────────────


def run_data_stage(
    store: StageStorage,
    paths: PipelinePaths,
    *,
    force: bool = False,
    mission_loader=None,
) -> dict[str, object]:
    """Build dataset manifest from real mission frames."""
    if mission_loader is None:
        raise RuntimeError("mission_loader is required for data stage")

    mission_input = mission_loader()
    source_fingerprint = _mission_source_fingerprint(mission_input)
    dataset_changed = False
    if store.exists(paths.data_key) and not force:
        existing_dataset = store.read_json(paths.data_key)
        existing_fingerprint = _dataset_source_fingerprint(existing_dataset)
        if existing_fingerprint == source_fingerprint:
            return _skip(
                "data",
                store.uri(paths.data_key),
                reason="source_unchanged",
                source_fingerprint=source_fingerprint,
            )
        dataset_changed = True

    valid_frames = [frame for frame in mission_input.frames if not frame.is_corrupted]
    corrupted_count = len(mission_input.frames) - len(valid_frames)
    if not valid_frames:
        raise RuntimeError("mission has no valid frames")

    positives = sum(1 for frame in valid_frames if frame.gt_person_present)

    payload: dict[str, object] = {
        "stage": "data",
        "created_at": _now_iso(),
        "mission_id": paths.mission_id,
        "ds": paths.ds,
        "source_uri": mission_input.source_uri,
        "source_fingerprint": source_fingerprint,
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
        "feature_hash": hashlib.sha256(
            f"{paths.mission_id}:{paths.ds}".encode()
        ).hexdigest()[:16],
    }
    store.write_json(paths.data_key, payload)
    result = _done("data", store.uri(paths.data_key))
    result["source_fingerprint"] = source_fingerprint
    result["recomputed_due_to_source_change"] = dataset_changed
    return result


# ── Stage 2: warmup (load deployed model + probe) ──────────────


def run_warmup_stage(
    store: StageStorage,
    paths: PipelinePaths,
    *,
    force: bool = False,
    model_probe=None,
) -> dict[str, object]:
    """Load the deployed detector, run a probe, write a model card.

    This stage does NOT train anything — weights are fixed. Its job is
    fail-fast: if the runtime cannot load the model, we stop here before
    spending compute on the full ``evaluate`` stage.
    """
    if not store.exists(paths.data_key):
        raise RuntimeError(f"dataset is missing: {store.uri(paths.data_key)}")

    dataset = store.read_json(paths.data_key)
    source_fingerprint = _dataset_source_fingerprint(dataset)
    model_changed = False
    if store.exists(paths.model_key) and not force:
        existing_model = store.read_json(paths.model_key)
        if existing_model.get("source_fingerprint") == source_fingerprint:
            return _skip(
                "warmup",
                store.uri(paths.model_key),
                reason="source_unchanged",
                source_fingerprint=source_fingerprint,
            )
        model_changed = True

    rows_total = _as_int(dataset.get("rows_total"), field_name="rows_total")
    rows_positive = _as_int(dataset.get("rows_positive"), field_name="rows_positive")
    if rows_total <= 0:
        raise RuntimeError("dataset has zero rows")
    if model_probe is None:
        raise RuntimeError("model_probe is required for warmup stage")

    model_meta = model_probe()
    if not isinstance(model_meta, dict):
        raise RuntimeError("model_probe must return a dict")

    class_ratio = rows_positive / rows_total
    payload: dict[str, object] = {
        "stage": "warmup",
        "created_at": _now_iso(),
        "mission_id": paths.mission_id,
        "ds": paths.ds,
        "source_fingerprint": source_fingerprint,
        "model_version": paths.model_version,
        "code_version": paths.code_version,
        "dataset_uri": store.uri(paths.data_key),
        "class_ratio": round(class_ratio, 6),
        "evaluation_count": _as_int(
            dataset.get("evaluation_count"), field_name="evaluation_count"
        ),
        "model_runtime": str(model_meta.get("runtime", "unknown")),
        "model_url": str(model_meta.get("model_url", "")),
        "model_ready": bool(model_meta.get("model_ready", True)),
        "checkpoint_hash": hashlib.sha256(
            f"{paths.model_version}:{paths.code_version}:{dataset}".encode()
        ).hexdigest()[:16],
    }
    store.write_json(paths.model_key, payload)
    result = _done("warmup", store.uri(paths.model_key))
    result["source_fingerprint"] = source_fingerprint
    result["recomputed_due_to_source_change"] = model_changed
    return result


# ── Stage 3: evaluate deployed detector on mission manifest ─────


def _metrics_from_evaluation(payload: dict[str, object]) -> dict[str, object]:
    """Pick metric fields out of an evaluation artifact for log output."""
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
            "passed",
        )
    }


def run_evaluate_stage(
    store: StageStorage,
    paths: PipelinePaths,
    *,
    force: bool = False,
    detector_predict=None,
) -> dict[str, object]:
    """Evaluate the deployed detector on all valid mission frames.

    This is the metric-generating stage: it runs the detector on every
    frame of the evaluation manifest, builds a confusion matrix, and records
    accuracy / precision / recall alongside the raw counts. It is intentionally NOT
    a quality gate on accuracy — GT coverage on production mission data
    is partial, so accuracy is an observability signal, not a pass/fail
    threshold. The only hard failure condition is a detector crash on a
    frame (captured in ``detector_errors``).
    """
    _require_evaluation_prerequisites(store, paths)
    if detector_predict is None:
        raise RuntimeError("detector_predict is required for evaluate stage")

    dataset = store.read_json(paths.data_key)
    source_fingerprint = _dataset_source_fingerprint(dataset)
    evaluation_changed = False
    if store.exists(paths.evaluation_key) and not force:
        existing_evaluation = store.read_json(paths.evaluation_key)
        if existing_evaluation.get("source_fingerprint") == source_fingerprint:
            return _skip(
                "evaluate",
                store.uri(paths.evaluation_key),
                reason="source_unchanged",
                source_fingerprint=source_fingerprint,
            )
        evaluation_changed = True

    _ensure_dataset_has_rows(dataset)
    gt_available = bool(dataset.get("gt_available", True))
    evaluation_manifest = _parse_evaluation_manifest(dataset)
    counts = _evaluate_validation_manifest(
        evaluation_manifest=evaluation_manifest,
        detector_predict=detector_predict,
    )

    payload: dict[str, object] = {
        "stage": "evaluate",
        "created_at": _now_iso(),
        "mission_id": paths.mission_id,
        "ds": paths.ds,
        "source_fingerprint": source_fingerprint,
        "dataset_uri": store.uri(paths.data_key),
        "model_uri": store.uri(paths.model_key),
        "tp": counts.tp,
        "tn": counts.tn,
        "fp": counts.fp,
        "fn": counts.fn,
        "detector_errors": counts.detector_errors,
        "accuracy": counts.accuracy if counts.total > 0 else 1.0,
        "precision": counts.precision,
        "recall": counts.recall,
        "gt_available": gt_available,
        "passed": counts.detector_errors == 0,
    }
    store.write_json(paths.evaluation_key, payload)

    if counts.detector_errors > 0:
        raise RuntimeError(
            f"evaluation failed: detector_errors={counts.detector_errors}"
        )
    result = _done("evaluate", store.uri(paths.evaluation_key))
    result["source_fingerprint"] = source_fingerprint
    result["recomputed_due_to_source_change"] = evaluation_changed
    result["metrics"] = _metrics_from_evaluation(payload)
    return result


# ── Stage 4: publish summary metrics to Postgres ───────────────


class BatchMetricsWriter(Protocol):
    """Minimal port for the publish stage (infrastructure-agnostic)."""

    def upsert(self, record: object) -> None:
        """Upsert one summary row into the backing store."""


def run_publish_stage(
    store: StageStorage,
    paths: PipelinePaths,
    *,
    metrics_writer,
    record_factory,
) -> dict[str, object]:
    """Read stage artifacts and upsert a summary row.

    This stage is the only place where the batch pipeline writes into
    the application Postgres. It is always safe to re-run — the
    repository uses ``ON CONFLICT ... DO UPDATE`` keyed on
    ``(ds, mission_id, model_version, code_version)``, so rerunning the
    DAG for the same ds updates the row in place, while rerunning for a
    different ds (e.g. via ``airflow dags backfill``) inserts a new row.
    """
    for required_key, label in (
        (paths.data_key, "dataset"),
        (paths.model_key, "model"),
        (paths.evaluation_key, "evaluation"),
    ):
        if not store.exists(required_key):
            raise RuntimeError(
                f"{label} artifact is missing: {store.uri(required_key)}"
            )

    dataset = store.read_json(paths.data_key)
    evaluation = store.read_json(paths.evaluation_key)

    record = record_factory(
        paths=paths,
        dataset=dataset,
        evaluation=evaluation,
        artifact_uris={
            "dataset_uri": store.uri(paths.data_key),
            "model_uri": store.uri(paths.model_key),
            "evaluation_uri": store.uri(paths.evaluation_key),
        },
    )
    metrics_writer.upsert(record)
    return {
        "stage": "publish",
        "status": "completed",
        "ds": paths.ds,
        "mission_id": paths.mission_id,
        "metrics": _metrics_from_evaluation(evaluation)
        | {
            "rows_total": dataset.get("rows_total"),
            "rows_positive": dataset.get("rows_positive"),
            "rows_corrupted": dataset.get("rows_corrupted"),
            "evaluation_count": dataset.get("evaluation_count"),
        },
    }


# ── Helpers ─────────────────────────────────────────────────────


def _skip(
    stage: str,
    uri: str,
    *,
    reason: str | None = None,
    source_fingerprint: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "stage": stage,
        "status": "idempotent_skip",
        "output_uri": uri,
    }
    if reason:
        result["skip_reason"] = reason
    if source_fingerprint:
        result["source_fingerprint"] = source_fingerprint
    return result


def _done(stage: str, uri: str) -> dict[str, object]:
    return {"stage": stage, "status": "completed", "output_uri": uri}


def _mission_source_fingerprint(mission_input: object) -> str:
    frames_raw = getattr(mission_input, "frames", [])
    records: list[tuple[int, str, bool, bool]] = []
    for frame in frames_raw:
        frame_id = int(getattr(frame, "frame_id", 0))
        image_uri = str(getattr(frame, "image_uri", ""))
        gt_person_present = bool(getattr(frame, "gt_person_present", False))
        is_corrupted = bool(getattr(frame, "is_corrupted", False))
        records.append((frame_id, image_uri, gt_person_present, is_corrupted))
    records.sort()
    payload = {
        "source_uri": str(getattr(mission_input, "source_uri", "")),
        "gt_available": bool(getattr(mission_input, "gt_available", False)),
        "frames": records,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _dataset_source_fingerprint(dataset: dict[str, object]) -> str:
    value = dataset.get("source_fingerprint")
    if isinstance(value, str) and value.strip():
        return value
    raise RuntimeError("source_fingerprint is missing in dataset artifact")


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


def _require_evaluation_prerequisites(
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


def _parse_evaluation_manifest(dataset: dict[str, object]) -> list[dict[str, object]]:
    evaluation_manifest_raw = dataset.get("evaluation_manifest")
    if not isinstance(evaluation_manifest_raw, list) or not evaluation_manifest_raw:
        raise RuntimeError("evaluation_manifest is missing in dataset artifact")
    if not all(isinstance(item, dict) for item in evaluation_manifest_raw):
        raise RuntimeError("evaluation_manifest item must be an object")
    return [item for item in evaluation_manifest_raw if isinstance(item, dict)]


def _evaluate_validation_manifest(
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


def print_result(result: dict[str, object]) -> None:
    """Print stage result to stdout in a log-friendly form.

    Metrics (if present) are printed as a readable key=value block so they
    land in Airflow task logs as actual numbers, not just an S3 URI pointing
    at a report JSON that nobody opens during a live defense.
    """
    stage = result.get("stage", "?")
    status = result.get("status", "?")
    header_parts = [f"[{stage}] status={status}"]
    if "output_uri" in result:
        header_parts.append(f"uri={result['output_uri']}")
    if "mission_id" in result:
        header_parts.append(f"mission={result['mission_id']}")
    if "ds" in result:
        header_parts.append(f"ds={result['ds']}")
    if "skip_reason" in result:
        header_parts.append(f"reason={result['skip_reason']}")
    if "source_fingerprint" in result:
        header_parts.append(f"fp={result['source_fingerprint']}")
    if result.get("recomputed_due_to_source_change"):
        header_parts.append("recomputed=true")
    print(" ".join(header_parts))
    metrics = result.get("metrics")
    if isinstance(metrics, dict) and metrics:
        for key, value in metrics.items():
            print(f"    {key}={value}")
    print(json.dumps(result, ensure_ascii=False, default=str))
