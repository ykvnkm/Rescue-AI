"""Unified CLI entry point for the Rescue-AI batch ML pipeline.

Supports four stages that run sequentially in Airflow:

    data  ->  warmup  ->  evaluate  ->  publish

``warmup`` loads the deployed detector and runs a probe (fail-fast on a
broken runtime before the heavy ``evaluate`` stage). ``evaluate`` runs
the detector over the mission evaluation manifest and records a confusion matrix.
Nothing here trains weights — training is out of scope for this DAG.

Usage::

    python -m rescue_ai.interfaces.cli.batch \\
        --stage data \\
        --mission-id demo_mission \\
        --ds 2026-03-01
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from typing import Any, Callable, TypedDict

from rescue_ai.application.pipeline_stages import (
    PipelinePaths,
    print_result,
    run_data_stage,
    run_evaluate_stage,
    run_publish_stage,
    run_warmup_stage,
)
from rescue_ai.config import get_settings
from rescue_ai.infrastructure.artifact_storage import (
    S3ArtifactBackendSettings,
    S3ArtifactStorage,
)
from rescue_ai.infrastructure.batch_metrics_repository import (
    BatchPipelineMetricsRecord,
    PostgresBatchMetricsRepository,
)
from rescue_ai.infrastructure.contract_loader import load_stream_contract
from rescue_ai.infrastructure.postgres_connection import PostgresDatabase
from rescue_ai.infrastructure.s3_mission_source import S3MissionSource
from rescue_ai.infrastructure.stage_store import S3StageStore
from rescue_ai.infrastructure.yolo_detector import YoloDetector

STAGES = ("data", "warmup", "evaluate", "publish")
DEFAULT_BATCH_OUTPUT_SUFFIX = "batch"
DEFAULT_SOURCE_FPS = 6.0
DEFAULT_MODEL_VERSION = "yolov8n_multiscale"
DEFAULT_CODE_VERSION = "v1"
_EXCLUDE_PREFIXES = frozenset({"batch"})


class ArtifactUris(TypedDict):
    """Resolved artifact URIs used to flatten stage outputs into one row."""

    dataset_uri: str
    model_uri: str
    evaluation_uri: str


def _build_metrics_record(
    *,
    paths: PipelinePaths,
    dataset: dict[str, object],
    evaluation: dict[str, object],
    artifact_uris: ArtifactUris,
) -> BatchPipelineMetricsRecord:
    """Flatten stage artifacts into a row for ``batch_pipeline_metrics``."""
    _ = artifact_uris

    def _int(value: object, default: int = 0) -> int:
        if isinstance(value, bool):
            return default
        if isinstance(value, (int, float)):
            return int(value)
        return default

    def _float(value: object, default: float = 0.0) -> float:
        if isinstance(value, bool):
            return default
        if isinstance(value, (int, float)):
            return float(value)
        return default

    def _bool(value: object, default: bool = False) -> bool:
        return bool(value) if isinstance(value, bool) else default

    tp = _int(evaluation.get("tp"))
    fp = _int(evaluation.get("fp"))
    fn = _int(evaluation.get("fn"))
    precision_default = (tp / (tp + fp)) if (tp + fp) > 0 else 1.0
    recall_default = (tp / (tp + fn)) if (tp + fn) > 0 else 1.0

    return BatchPipelineMetricsRecord(
        ds=paths.ds,
        mission_id=paths.mission_id,
        model_version=paths.model_version,
        code_version=paths.code_version,
        rows_total=_int(dataset.get("rows_total")),
        rows_positive=_int(dataset.get("rows_positive")),
        rows_corrupted=_int(dataset.get("rows_corrupted")),
        evaluation_count=_int(dataset.get("evaluation_count")),
        tp=tp,
        tn=_int(evaluation.get("tn")),
        fp=fp,
        fn=fn,
        detector_errors=_int(evaluation.get("detector_errors")),
        accuracy=_float(evaluation.get("accuracy")),
        precision=_float(evaluation.get("precision"), default=precision_default),
        recall=_float(evaluation.get("recall"), default=recall_default),
        gt_available=_bool(evaluation.get("gt_available")),
        validate_passed=_bool(evaluation.get("passed")),
    )


def parse_args() -> argparse.Namespace:
    """Parse unified pipeline CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Rescue-AI ML pipeline (data/warmup/evaluate/publish)"
    )
    parser.add_argument(
        "--stage", required=True, choices=STAGES, help="Pipeline stage to run"
    )
    parser.add_argument("--mission-id")
    parser.add_argument("--all-missions", action="store_true")
    parser.add_argument("--mission-ids-csv", default="")
    parser.add_argument("--ds", required=True)
    parser.add_argument("--model-version", default=DEFAULT_MODEL_VERSION)
    parser.add_argument("--code-version", default=DEFAULT_CODE_VERSION)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.all_missions and args.mission_id:
        parser.error("Use either --mission-id or --all-missions, not both")
    if not args.all_missions and not args.mission_id:
        parser.error("Either --mission-id or --all-missions is required")
    return args


def _build_s3_settings() -> S3ArtifactBackendSettings:
    settings = get_settings()
    if not settings.storage.s3_bucket:
        raise ValueError("ARTIFACTS_S3_BUCKET is required for batch pipeline")
    return S3ArtifactBackendSettings(
        endpoint=settings.storage.s3_endpoint,
        region=settings.storage.s3_region,
        access_key_id=settings.storage.s3_access_key_id,
        secret_access_key=settings.storage.s3_secret_access_key,
        bucket=settings.storage.s3_bucket,
        prefix=settings.storage.s3_prefix,
    )


def build_stage_store() -> S3StageStore:
    """Build S3 stage artifact store from settings."""
    return S3StageStore(_build_s3_settings())


def build_artifact_store() -> S3ArtifactStorage:
    """Build S3 artifact store for batch outputs."""
    return S3ArtifactStorage(settings=_build_s3_settings())


def build_source() -> S3MissionSource:
    """Build S3 mission source for batch processing."""
    settings = get_settings()
    return S3MissionSource(
        settings=_build_s3_settings(),
        source_prefix=settings.storage.s3_prefix,
        fps=DEFAULT_SOURCE_FPS,
    )


def _join_s3(*parts: str) -> str:
    return "/".join(part.strip("/") for part in parts if part.strip("/"))


def _has_any_keys(client: Any, *, bucket: str, prefix: str) -> bool:
    response = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    return bool(response.get("Contents"))


def _mission_has_input_for_ds(
    *,
    client: Any,
    bucket: str,
    base_prefix: str,
    mission_id: str,
    ds: str,
) -> bool:
    roots = [
        _join_s3(base_prefix, mission_id, ds),
        _join_s3(base_prefix, f"mission={mission_id}", f"ds={ds}"),
        _join_s3(base_prefix, mission_id),
    ]
    for root in roots:
        for subdir in ("frames", "images"):
            if _has_any_keys(
                client,
                bucket=bucket,
                prefix=f"{_join_s3(root, subdir)}/",
            ):
                return True
    return False


def _mission_manifest_key(batch_prefix: str, ds: str) -> str:
    """Return the S3 key for the cached per-ds mission discovery manifest."""
    prefix = batch_prefix.strip("/")
    root = f"ml_pipeline/ds={ds}/missions.json"
    return f"{prefix}/{root}" if prefix else root


def _resolve_mission_ids(
    args: argparse.Namespace,
    *,
    store: S3StageStore,
    batch_prefix: str,
) -> list[str]:
    """Return the list of missions to process for this DAG run.

    Discovery is expensive (paginated ``list_objects_v2`` + 2 HEAD per
    candidate), so we cache the result in a small JSON manifest keyed by
    ``ds``. The first stage of the DAG (``data``) writes the manifest; every
    downstream stage (``warmup``/``evaluate``/``publish``) reads it with a
    single ``get_object``. This also pins the mission set for the whole run
    — if new missions appear in S3 mid-run, they are deferred to the next
    ``ds`` instead of racing between stages.
    """
    manifest_key = _mission_manifest_key(batch_prefix, args.ds)
    stage = args.stage
    force_refresh = stage == "data" and args.force

    if store.exists(manifest_key) and not force_refresh:
        cached = store.read_json(manifest_key)
        ids_raw = cached.get("mission_ids") or []
        if isinstance(ids_raw, list):
            return [str(item) for item in ids_raw if isinstance(item, str)]

    if stage != "data":
        raise RuntimeError(
            f"mission manifest is missing: {store.uri(manifest_key)}. "
            "Run the `data` stage first so discovery is pinned for this ds."
        )

    discovered = _discover_missions_for_ds(args.ds)
    store.write_json(
        manifest_key,
        {
            "ds": args.ds,
            "mission_ids": discovered,
            "count": len(discovered),
        },
    )
    return discovered


def _discover_missions_for_ds(ds: str) -> list[str]:
    settings = get_settings()
    s3_settings = _build_s3_settings()
    bucket = s3_settings.bucket
    if not bucket:
        raise ValueError("ARTIFACTS_S3_BUCKET is required for mission discovery")
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("boto3 is required for mission discovery") from exc

    client = boto3.client(
        "s3",
        endpoint_url=s3_settings.endpoint,
        region_name=s3_settings.region,
        aws_access_key_id=s3_settings.access_key_id,
        aws_secret_access_key=s3_settings.secret_access_key,
    )
    prefix = settings.storage.s3_prefix.strip("/")
    search_prefix = f"{prefix}/" if prefix else ""

    paginator = client.get_paginator("list_objects_v2")
    found: list[str] = []
    for page in paginator.paginate(
        Bucket=bucket,
        Prefix=search_prefix,
        Delimiter="/",
    ):
        for common in page.get("CommonPrefixes", []) or []:
            segment = common["Prefix"].rstrip("/").split("/")[-1]
            if (
                segment
                and segment not in _EXCLUDE_PREFIXES
                and _mission_has_input_for_ds(
                    client=client,
                    bucket=bucket,
                    base_prefix=prefix,
                    mission_id=segment,
                    ds=ds,
                )
            ):
                found.append(segment)
    return sorted(set(found))


def _run_stage_data(
    args: argparse.Namespace,
    *,
    settings,
    store: S3StageStore,
    paths: PipelinePaths,
) -> dict[str, object]:
    _ = settings
    source = build_source()
    return run_data_stage(
        store,
        paths,
        force=args.force,
        mission_loader=lambda: source.load(args.mission_id, args.ds),
    )


def _run_stage_warmup(
    args: argparse.Namespace,
    *,
    settings,
    store: S3StageStore,
    paths: PipelinePaths,
) -> dict[str, object]:
    _ = settings
    contract = load_stream_contract()
    detector = YoloDetector(config=contract.inference, model_version=args.model_version)

    def _model_probe() -> dict[str, object]:
        detector.warmup()
        return {
            "runtime": detector.runtime_name(),
            "model_url": contract.inference.model_url,
            "model_ready": True,
        }

    return run_warmup_stage(
        store,
        paths,
        force=args.force,
        model_probe=_model_probe,
    )


def _run_stage_evaluate(
    args: argparse.Namespace,
    *,
    settings,
    store: S3StageStore,
    paths: PipelinePaths,
) -> dict[str, object]:
    _ = settings
    contract = load_stream_contract()
    detector = YoloDetector(config=contract.inference, model_version=args.model_version)
    val_tmp = Path(tempfile.mkdtemp(prefix="rescue_ai_eval_"))
    s3_settings = _build_s3_settings()

    def _detector_predict(image_uri: str) -> bool:
        if image_uri.startswith("s3://"):
            import boto3

            path_part = image_uri[5:]
            bucket, _, key = path_part.partition("/")
            local_path = val_tmp / Path(key).name
            if not local_path.exists():
                client = boto3.client(
                    "s3",
                    endpoint_url=s3_settings.endpoint,
                    region_name=s3_settings.region,
                    aws_access_key_id=s3_settings.access_key_id,
                    aws_secret_access_key=s3_settings.secret_access_key,
                )
                client.download_file(bucket, key, str(local_path))
            return bool(detector.detect(str(local_path)))
        return bool(detector.detect(image_uri))

    return run_evaluate_stage(
        store,
        paths,
        force=args.force,
        detector_predict=_detector_predict,
    )


def _run_stage_publish(
    _args: argparse.Namespace,
    *,
    settings,
    store: S3StageStore,
    paths: PipelinePaths,
) -> dict[str, object]:
    dsn = settings.database.dsn.strip()
    if not dsn:
        raise ValueError("DB_DSN is required for publish stage")
    metrics_writer = PostgresBatchMetricsRepository(
        db=PostgresDatabase(dsn=dsn, schema="app")
    )
    return run_publish_stage(
        store,
        paths,
        metrics_writer=metrics_writer,
        record_factory=_build_metrics_record,
    )


def main() -> None:
    """Run a single stage for one mission, or for all missions of ``ds``."""
    args = parse_args()
    settings = get_settings()

    store = build_stage_store()
    root_prefix = settings.storage.s3_prefix.strip("/")
    batch_prefix = (
        f"{root_prefix}/{DEFAULT_BATCH_OUTPUT_SUFFIX}"
        if root_prefix
        else DEFAULT_BATCH_OUTPUT_SUFFIX
    )
    mission_ids = (
        [args.mission_id]
        if args.mission_id
        else _resolve_mission_ids(args, store=store, batch_prefix=batch_prefix)
    )
    mission_ids_csv = args.mission_ids_csv.strip()
    if mission_ids_csv:
        requested = {mission_id.strip() for mission_id in mission_ids_csv.split(",")}
        requested = {mission_id for mission_id in requested if mission_id}
        missing = requested - set(mission_ids)
        if missing:
            raise ValueError(
                "Requested missions have no input for ds="
                f"{args.ds}: {', '.join(sorted(missing))}"
            )
        mission_ids = sorted(requested)
    if not mission_ids:
        raise ValueError(f"No missions discovered with input frames for ds={args.ds}")

    stage_handlers: dict[
        str,
        Callable[..., dict[str, object]],
    ] = {
        "data": _run_stage_data,
        "warmup": _run_stage_warmup,
        "evaluate": _run_stage_evaluate,
        "publish": _run_stage_publish,
    }
    for mission_id in mission_ids:
        paths = PipelinePaths(
            prefix=batch_prefix,
            mission_id=mission_id,
            ds=args.ds,
            model_version=args.model_version,
            code_version=args.code_version,
        )
        run_args = argparse.Namespace(**vars(args))
        run_args.mission_id = mission_id
        result = stage_handlers[args.stage](
            run_args,
            settings=settings,
            store=store,
            paths=paths,
        )
        print_result(result)


if __name__ == "__main__":
    main()
