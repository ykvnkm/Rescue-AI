"""Unified CLI entry point for the Rescue-AI batch ML pipeline.

Supports four stages that run sequentially in Airflow:

    data  ->  train  ->  validate  ->  inference

Usage::

    python -m rescue_ai.interfaces.cli.batch \\
        --stage data \\
        --mission-id demo_mission \\
        --ds 2026-03-01
"""

from __future__ import annotations

import argparse
import importlib
import tempfile
from pathlib import Path
from typing import Callable, TypedDict

from rescue_ai.application.batch_dtos import BatchRunRequest
from rescue_ai.application.batch_runner import (
    MissionBatchRunner,
    MissionBatchRunnerDeps,
)
from rescue_ai.application.pilot_service import PilotService
from rescue_ai.application.pipeline_stages import (
    PipelinePaths,
    print_result,
    run_data_stage,
    run_inference_stage,
    run_publish_stage,
    run_train_stage,
    run_validate_stage,
)
from rescue_ai.config import get_settings
from rescue_ai.domain.ports import DetectorPort, ReportMetadataPayload
from rescue_ai.domain.value_objects import AlertRuleConfig
from rescue_ai.infrastructure.artifact_storage import (
    S3ArtifactBackendSettings,
    S3ArtifactStorage,
    build_s3_storage,
)
from rescue_ai.infrastructure.batch_metrics_repository import (
    BatchPipelineMetricsRecord,
    PostgresBatchMetricsRepository,
)
from rescue_ai.infrastructure.contract_loader import load_stream_contract
from rescue_ai.infrastructure.pilot_engine import PilotMissionEngine
from rescue_ai.infrastructure.postgres_connection import PostgresDatabase
from rescue_ai.infrastructure.postgres_repositories import (
    EpisodeProjectionSettings,
    PostgresAlertRepository,
    PostgresFrameEventRepository,
    PostgresMissionRepository,
)
from rescue_ai.infrastructure.s3_mission_source import S3MissionSource
from rescue_ai.infrastructure.stage_store import S3StageStore
from rescue_ai.infrastructure.status_store import JsonStatusStore, PostgresStatusStore
from rescue_ai.infrastructure.yolo_detector import YoloDetector

STAGES = ("data", "train", "validate", "inference", "publish")
DEFAULT_MODEL_VERSION = "yolov8n_baseline_multiscale"
DEFAULT_CODE_VERSION = "main"
DEFAULT_BATCH_OUTPUT_SUFFIX = "batch"
DEFAULT_SOURCE_FPS = 6.0


class ArtifactUris(TypedDict):
    """Resolved artifact URIs used to flatten stage outputs into one row."""

    dataset_uri: str
    model_uri: str
    validation_uri: str
    inference_uri: str


class PilotMissionEngineFactory:
    """Creates isolated Postgres-backed mission engine instances per run."""

    def create(
        self,
        alert_rules: AlertRuleConfig,
        report_metadata: ReportMetadataPayload,
    ) -> PilotMissionEngine:
        settings = get_settings()
        dsn = settings.database.dsn.strip()
        if not dsn:
            raise ValueError("DB_DSN is required")

        postgres_db = PostgresDatabase(dsn=dsn, schema="app")
        episode_settings = EpisodeProjectionSettings(
            gt_gap_end_sec=alert_rules.gt_gap_end_sec,
            match_tolerance_sec=alert_rules.match_tolerance_sec,
        )
        pilot = PilotService(
            dependencies=PilotService.Dependencies(
                mission_repository=PostgresMissionRepository(postgres_db),
                alert_repository=PostgresAlertRepository(
                    postgres_db,
                    episode_settings=episode_settings,
                ),
                frame_event_repository=PostgresFrameEventRepository(
                    postgres_db,
                    episode_settings=episode_settings,
                ),
                artifact_storage=build_s3_storage(settings.storage),
            ),
            alert_rules=alert_rules,
        )
        pilot.set_report_metadata(report_metadata)
        return PilotMissionEngine(pilot=pilot)

    def factory_name(self) -> str:
        return "pilot-postgres"


def _build_metrics_record(
    *,
    paths: PipelinePaths,
    dataset: dict[str, object],
    validation: dict[str, object],
    inference: dict[str, object],
    artifact_uris: ArtifactUris,
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

    def _str(value: object, default: str = "") -> str:
        return value if isinstance(value, str) else default

    return BatchPipelineMetricsRecord(
        ds=paths.ds,
        mission_id=paths.mission_id,
        model_version=paths.model_version,
        code_version=paths.code_version,
        rows_total=_int(dataset.get("rows_total")),
        rows_positive=_int(dataset.get("rows_positive")),
        rows_corrupted=_int(dataset.get("rows_corrupted")),
        train_count=_int(dataset.get("train_count")),
        val_count=_int(dataset.get("val_count")),
        samples_total=_int(validation.get("samples_total")),
        tp=_int(validation.get("tp")),
        tn=_int(validation.get("tn")),
        fp=_int(validation.get("fp")),
        fn=_int(validation.get("fn")),
        detector_errors=_int(validation.get("detector_errors")),
        accuracy=_float(validation.get("accuracy")),
        gt_available=_bool(validation.get("gt_available")),
        validate_passed=_bool(validation.get("passed")),
        inference_status=_str(inference.get("status"), default="unknown"),
        inference_run_key=_str(inference.get("run_key")),
        dataset_uri=artifact_uris["dataset_uri"],
        model_uri=artifact_uris["model_uri"],
        validation_uri=artifact_uris["validation_uri"],
        inference_uri=artifact_uris["inference_uri"],
    )


def parse_args() -> argparse.Namespace:
    """Parse unified pipeline CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Rescue-AI ML pipeline (data/train/validate/inference)"
    )
    parser.add_argument(
        "--stage", required=True, choices=STAGES, help="Pipeline stage to run"
    )
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--ds", required=True)
    parser.add_argument("--model-version", default=DEFAULT_MODEL_VERSION)
    parser.add_argument("--code-version", default=DEFAULT_CODE_VERSION)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


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
    )


def build_stage_store() -> S3StageStore:
    """Build S3 stage artifact store from settings."""
    return S3StageStore(_build_s3_settings())


def build_status_store() -> PostgresStatusStore | JsonStatusStore:
    """Build run status store; falls back to local JSON if Postgres is unreachable."""
    settings = get_settings()
    dsn = settings.database.dsn.strip()
    if not dsn:
        raise ValueError("DB_DSN is required")
    try:
        psycopg_module = importlib.import_module("psycopg")
        connection = psycopg_module.connect(dsn, connect_timeout=5)
        connection.close()
        return PostgresStatusStore(db=PostgresDatabase(dsn=dsn, schema="app"))
    except (ImportError, OSError, TimeoutError) as exc:
        print(f"[WARN] Postgres unavailable ({exc}), using local JSON status store")
    return JsonStatusStore(path=Path("/tmp/rescue_ai_status"))


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


def build_runner(detector: DetectorPort) -> MissionBatchRunner:
    """Build batch runner with all dependencies wired."""
    return MissionBatchRunner(
        MissionBatchRunnerDeps(
            source=build_source(),
            detector=detector,
            artifacts=build_artifact_store(),
            statuses=build_status_store(),
            engine_factory=PilotMissionEngineFactory(),
        )
    )


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


def _run_stage_train(
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

    return run_train_stage(
        store,
        paths,
        force=args.force,
        model_probe=_model_probe,
    )


def _run_stage_validate(
    args: argparse.Namespace,
    *,
    settings,
    store: S3StageStore,
    paths: PipelinePaths,
) -> dict[str, object]:
    _ = settings
    contract = load_stream_contract()
    detector = YoloDetector(config=contract.inference, model_version=args.model_version)
    val_tmp = Path(tempfile.mkdtemp(prefix="rescue_ai_val_"))
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

    return run_validate_stage(
        store,
        paths,
        force=args.force,
        detector_predict=_detector_predict,
    )


def _run_stage_inference(
    args: argparse.Namespace,
    *,
    settings,
    store: S3StageStore,
    paths: PipelinePaths,
) -> dict[str, object]:
    def _runner_factory() -> tuple[MissionBatchRunner, BatchRunRequest]:
        contract = load_stream_contract(service_version=settings.app.service_version)
        detector = YoloDetector(
            config=contract.inference, model_version=args.model_version
        )
        runner = build_runner(detector=detector)
        request = BatchRunRequest(
            mission_id=args.mission_id,
            ds=args.ds,
            config_hash=contract.config_hash,
            model_version=args.model_version,
            code_version=args.code_version,
            alert_rules=contract.alert_rules,
            force=args.force,
        )
        return runner, request

    return run_inference_stage(
        store, paths, force=args.force, runner_factory=_runner_factory
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
    """Run a single pipeline stage."""
    args = parse_args()
    settings = get_settings()

    store = build_stage_store()
    root_prefix = settings.storage.s3_prefix.strip("/")
    batch_prefix = (
        f"{root_prefix}/{DEFAULT_BATCH_OUTPUT_SUFFIX}"
        if root_prefix
        else DEFAULT_BATCH_OUTPUT_SUFFIX
    )
    paths = PipelinePaths(
        prefix=batch_prefix,
        mission_id=args.mission_id,
        ds=args.ds,
        model_version=args.model_version,
        code_version=args.code_version,
    )

    stage_handlers: dict[
        str,
        Callable[..., dict[str, object]],
    ] = {
        "data": _run_stage_data,
        "train": _run_stage_train,
        "validate": _run_stage_validate,
        "inference": _run_stage_inference,
        "publish": _run_stage_publish,
    }
    result = stage_handlers[args.stage](
        args,
        settings=settings,
        store=store,
        paths=paths,
    )

    print_result(result)


if __name__ == "__main__":
    main()
