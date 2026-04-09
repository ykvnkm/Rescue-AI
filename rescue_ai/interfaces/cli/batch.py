"""CLI entry point for the batch ML pipeline.

Runs a single stage (``prepare_dataset`` / ``evaluate_model`` /
``publish_metrics``) over every mission discovered for ``--ds``.
Invoked one stage at a time by the Airflow DAG.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from typing import Any, Callable

from rescue_ai.application.pipeline_stages import (
    PipelinePaths,
    print_result,
    run_evaluate_model_stage,
    run_prepare_dataset_stage,
    run_publish_metrics_stage,
)
from rescue_ai.config import get_settings
from rescue_ai.infrastructure.artifact_storage import S3ArtifactBackendSettings
from rescue_ai.infrastructure.batch_metrics_repository import (
    BatchPipelineMetricsRecord,
    PostgresBatchMetricsRepository,
)
from rescue_ai.infrastructure.contract_loader import load_stream_contract
from rescue_ai.infrastructure.postgres_connection import PostgresDatabase
from rescue_ai.infrastructure.s3_mission_source import S3MissionSource
from rescue_ai.infrastructure.stage_store import S3StageStore
from rescue_ai.infrastructure.yolo_detector import YoloDetector

STAGES = ("prepare_dataset", "evaluate_model", "publish_metrics")
DEFAULT_BATCH_OUTPUT_SUFFIX = "batch"
DEFAULT_SOURCE_FPS = 6.0


def _build_metrics_record(
    *,
    paths: PipelinePaths,
    dataset: dict[str, object],
    evaluation: dict[str, object],
) -> BatchPipelineMetricsRecord:
    """Flatten stage artifacts into a row for ``batch_pipeline_metrics``."""

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

    return BatchPipelineMetricsRecord(
        ds=paths.ds,
        mission_id=paths.mission_id,
        model_version=paths.model_version,
        rows_total=_int(dataset.get("rows_total")),
        rows_positive=_int(dataset.get("rows_positive")),
        rows_corrupted=_int(dataset.get("rows_corrupted")),
        evaluation_count=_int(dataset.get("evaluation_count")),
        tp=_int(evaluation.get("tp")),
        tn=_int(evaluation.get("tn")),
        fp=_int(evaluation.get("fp")),
        fn=_int(evaluation.get("fn")),
        detector_errors=_int(evaluation.get("detector_errors")),
        accuracy=_float(evaluation.get("accuracy")),
        precision=_float(evaluation.get("precision")),
        recall=_float(evaluation.get("recall")),
        gt_available=_bool(evaluation.get("gt_available")),
        validate_passed=_bool(evaluation.get("passed")),
    )


def parse_args() -> argparse.Namespace:
    """Parse pipeline CLI arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Rescue-AI batch ML pipeline (prepare_dataset / evaluate_model / "
            "publish_metrics)"
        )
    )
    parser.add_argument("--stage", required=True, choices=STAGES)
    parser.add_argument(
        "--ds", required=True, help="Partition date in YYYY-MM-DD format"
    )
    parser.add_argument(
        "--mission-ids-csv",
        default="",
        help="Optional comma-separated allow-list of mission IDs",
    )
    parser.add_argument(
        "--model-version",
        default=None,
        help=(
            "Model version tag. Defaults to "
            "rescue_ai.config.BatchSettings.default_model_version."
        ),
    )
    return parser.parse_args()


# ── S3 wiring ───────────────────────────────────────────────────


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


def _build_s3_client() -> Any:
    """Return a fresh boto3 S3 client wired to the artifacts bucket."""
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("boto3 is required for batch pipeline") from exc

    s3_settings = _build_s3_settings()
    return boto3.client(
        "s3",
        endpoint_url=s3_settings.endpoint,
        region_name=s3_settings.region,
        aws_access_key_id=s3_settings.access_key_id,
        aws_secret_access_key=s3_settings.secret_access_key,
    )


def build_stage_store() -> S3StageStore:
    """Build S3 stage artifact store from settings."""
    return S3StageStore(_build_s3_settings())


def build_source() -> S3MissionSource:
    """Build the S3 mission source used by ``prepare_dataset``."""
    settings = get_settings()
    return S3MissionSource(
        settings=_build_s3_settings(),
        source_prefix=settings.storage.s3_prefix,
        fps=DEFAULT_SOURCE_FPS,
    )


def _join_s3(*parts: str) -> str:
    return "/".join(part.strip("/") for part in parts if part.strip("/"))


# ── Mission discovery (one fresh LIST per stage) ────────────────


def _list_input_missions(client: Any, *, ds: str) -> list[str]:
    """List mission IDs that have a frames/ folder under ``ds=<ds>/``."""
    settings = get_settings()
    bucket = settings.storage.s3_bucket
    prefix = settings.storage.s3_prefix.strip("/")
    search_prefix = _join_s3(prefix, f"ds={ds}") + "/"

    paginator = client.get_paginator("list_objects_v2")
    found: list[str] = []
    for page in paginator.paginate(
        Bucket=bucket,
        Prefix=search_prefix,
        Delimiter="/",
    ):
        for common in page.get("CommonPrefixes", []) or []:
            segment = common["Prefix"].rstrip("/").split("/")[-1]
            if not segment:
                continue
            frames_prefix = _join_s3(search_prefix, segment, "frames") + "/"
            if _has_any_keys(client, bucket=bucket, prefix=frames_prefix):
                found.append(segment)
    return sorted(set(found))


def _list_output_missions_with_artifact(
    client: Any,
    *,
    ds: str,
    batch_prefix: str,
    artifact_filename: str,
) -> list[str]:
    """List mission IDs that already have ``artifact_filename`` under ``ds``.

    Used by ``evaluate_model`` (looking for ``dataset.json``) and
    ``publish_metrics`` (looking for ``evaluation_<mv>.json``). Each
    stage operates on the intersection of its discovery and the upstream
    artifact existence — that is the canonical "stage runs over what its
    upstream produced" pattern.
    """
    settings = get_settings()
    bucket = settings.storage.s3_bucket
    search_prefix = _join_s3(batch_prefix, "ml_pipeline", f"ds={ds}") + "/"

    paginator = client.get_paginator("list_objects_v2")
    found: list[str] = []
    for page in paginator.paginate(
        Bucket=bucket,
        Prefix=search_prefix,
        Delimiter="/",
    ):
        for common in page.get("CommonPrefixes", []) or []:
            segment = common["Prefix"].rstrip("/").split("/")[-1]
            if not segment.startswith("mission="):
                continue
            mission_id = segment.removeprefix("mission=")
            artifact_prefix = _join_s3(search_prefix, segment, artifact_filename)
            if _has_any_keys(client, bucket=bucket, prefix=artifact_prefix):
                found.append(mission_id)
    return sorted(set(found))


def _has_any_keys(client: Any, *, bucket: str, prefix: str) -> bool:
    response = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    return bool(response.get("Contents"))


def _evaluation_filename(model_version: str) -> str:
    """Return the evaluation filename for the given model version."""
    paths = PipelinePaths(
        prefix="",
        mission_id="x",
        ds="x",
        model_version=model_version,
    )
    return paths.evaluation_key.rsplit("/", maxsplit=1)[-1]


def _resolve_mission_ids(
    args: argparse.Namespace,
    *,
    client: Any,
    batch_prefix: str,
    model_version: str,
) -> list[str]:
    """Discover the mission set this stage should iterate over."""
    if args.stage == "prepare_dataset":
        return _list_input_missions(client, ds=args.ds)
    if args.stage == "evaluate_model":
        return _list_output_missions_with_artifact(
            client,
            ds=args.ds,
            batch_prefix=batch_prefix,
            artifact_filename="dataset.json",
        )
    return _list_output_missions_with_artifact(
        client,
        ds=args.ds,
        batch_prefix=batch_prefix,
        artifact_filename=_evaluation_filename(model_version),
    )


# ── Stage handlers ──────────────────────────────────────────────


def _run_prepare_dataset(
    args: argparse.Namespace,
    *,
    settings,
    store: S3StageStore,
    paths: PipelinePaths,
) -> dict[str, object]:
    _ = settings
    source = build_source()
    return run_prepare_dataset_stage(
        store,
        paths,
        mission_loader=lambda: source.load(paths.mission_id, args.ds),
    )


def _run_evaluate_model(
    args: argparse.Namespace,
    *,
    settings,
    store: S3StageStore,
    paths: PipelinePaths,
) -> dict[str, object]:
    _ = (settings, args)
    contract = load_stream_contract()
    detector = YoloDetector(
        config=contract.inference, model_version=paths.model_version
    )
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

    return run_evaluate_model_stage(
        store,
        paths,
        detector_predict=_detector_predict,
    )


def _run_publish_metrics(
    _args: argparse.Namespace,
    *,
    settings,
    store: S3StageStore,
    paths: PipelinePaths,
) -> dict[str, object]:
    dsn = settings.database.dsn.strip()
    if not dsn:
        raise ValueError("DB_DSN is required for publish_metrics stage")
    metrics_writer = PostgresBatchMetricsRepository(
        db=PostgresDatabase(dsn=dsn, schema="app")
    )
    return run_publish_metrics_stage(
        store,
        paths,
        metrics_writer=metrics_writer,
        record_factory=_build_metrics_record,
    )


# ── Main entry point ────────────────────────────────────────────


def main() -> None:
    """Run a single stage over every mission discovered for ``ds``."""
    args = parse_args()
    settings = get_settings()
    model_version = args.model_version or settings.batch.default_model_version

    store = build_stage_store()
    root_prefix = settings.storage.s3_prefix.strip("/")
    batch_prefix = (
        f"{root_prefix}/{DEFAULT_BATCH_OUTPUT_SUFFIX}"
        if root_prefix
        else DEFAULT_BATCH_OUTPUT_SUFFIX
    )

    client = _build_s3_client()
    mission_ids = _resolve_mission_ids(
        args,
        client=client,
        batch_prefix=batch_prefix,
        model_version=model_version,
    )

    mission_ids_csv = args.mission_ids_csv.strip()
    if mission_ids_csv:
        requested = {
            item.strip() for item in mission_ids_csv.split(",") if item.strip()
        }
        mission_ids = sorted(set(mission_ids) & requested)

    if not mission_ids:
        print(f"[{args.stage}] no missions discovered for ds={args.ds}, nothing to do")
        return

    stage_handlers: dict[
        str,
        Callable[..., dict[str, object]],
    ] = {
        "prepare_dataset": _run_prepare_dataset,
        "evaluate_model": _run_evaluate_model,
        "publish_metrics": _run_publish_metrics,
    }
    handler = stage_handlers[args.stage]

    for mission_id in mission_ids:
        paths = PipelinePaths(
            prefix=batch_prefix,
            mission_id=mission_id,
            ds=args.ds,
            model_version=model_version,
        )
        result = handler(args, settings=settings, store=store, paths=paths)
        print_result(result)


if __name__ == "__main__":
    main()
