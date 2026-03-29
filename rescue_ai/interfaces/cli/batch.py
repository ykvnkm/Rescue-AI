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
from rescue_ai.infrastructure.contract_loader import load_stream_contract
from rescue_ai.infrastructure.pilot_engine import PilotMissionEngine
from rescue_ai.infrastructure.postgres_connection import (
    PostgresDatabase,
    dsn_with_search_path,
)
from rescue_ai.infrastructure.postgres_repositories import (
    EpisodeProjectionSettings,
    PostgresAlertRepository,
    PostgresFrameEventRepository,
    PostgresMissionRepository,
)
from rescue_ai.infrastructure.s3_mission_source import S3MissionSource
from rescue_ai.infrastructure.stage_store import S3StageStore
from rescue_ai.infrastructure.status_store import PostgresStatusStore
from rescue_ai.infrastructure.yolo_detector import YoloDetector

STAGES = ("data", "train", "validate", "inference")
DEFAULT_MODEL_VERSION = "yolov8n_baseline_multiscale"
DEFAULT_CODE_VERSION = "main"
DEFAULT_BATCH_OUTPUT_SUFFIX = "batch"
DEFAULT_SOURCE_FPS = 6.0


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
        app_dsn = dsn_with_search_path(dsn, "app")

        postgres_db = PostgresDatabase(dsn=app_dsn)
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
    parser.add_argument("--min-accuracy", type=float, default=0.75)
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


def build_status_store() -> PostgresStatusStore:
    """Build Postgres run status store."""
    settings = get_settings()
    dsn = settings.database.dsn.strip()
    if not dsn:
        raise ValueError("DB_DSN is required")
    app_dsn = dsn_with_search_path(dsn, "app")

    return PostgresStatusStore(db=PostgresDatabase(dsn=app_dsn))


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

    if args.stage == "data":
        source = build_source()
        result = run_data_stage(
            store,
            paths,
            force=args.force,
            mission_loader=lambda: source.load(args.mission_id, args.ds),
        )

    elif args.stage == "train":
        contract = load_stream_contract()
        detector = YoloDetector(
            config=contract.inference, model_version=args.model_version
        )

        def _model_probe() -> dict[str, object]:
            detector.warmup()
            return {
                "runtime": detector.runtime_name(),
                "model_url": contract.inference.model_url,
                "model_ready": True,
            }

        result = run_train_stage(
            store,
            paths,
            force=args.force,
            model_probe=_model_probe,
        )

    elif args.stage == "validate":
        contract = load_stream_contract()
        detector = YoloDetector(
            config=contract.inference, model_version=args.model_version
        )

        def _detector_predict(image_uri: str) -> bool:
            return bool(detector.detect(image_uri))

        result = run_validate_stage(
            store,
            paths,
            force=args.force,
            min_accuracy=args.min_accuracy,
            detector_predict=_detector_predict,
        )

    elif args.stage == "inference":

        def _runner_factory() -> tuple[MissionBatchRunner, BatchRunRequest]:
            contract = load_stream_contract(
                service_version=settings.app.service_version
            )
            det = YoloDetector(
                config=contract.inference, model_version=args.model_version
            )
            runner = build_runner(detector=det)
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

        result = run_inference_stage(
            store, paths, force=args.force, runner_factory=_runner_factory
        )
    else:
        raise ValueError(f"Unknown stage: {args.stage}")

    print_result(result)


if __name__ == "__main__":
    main()
