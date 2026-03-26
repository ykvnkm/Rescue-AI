"""Unified CLI entry point for the Rescue-AI batch ML pipeline.

Supports four stages that run sequentially in Airflow:

    data  →  train  →  validate  →  inference

Usage::

    python -m rescue_ai.interfaces.cli.batch \\
        --stage data \\
        --mission-id demo_mission \\
        --ds 2026-03-01 \\
        --model-version yolov8n_baseline_multiscale \\
        --code-version dev
"""

from __future__ import annotations

import argparse

from rescue_ai.application.batch_runner import (
    BatchRunRequest,
    MissionBatchRunner,
    MissionBatchRunnerDeps,
)
from rescue_ai.application.pipeline_stages import (
    PipelinePaths,
    print_result,
    run_data_stage,
    run_inference_stage,
    run_train_stage,
    run_validate_stage,
)
from rescue_ai.config import get_settings
from rescue_ai.infrastructure.contract_loader import load_stream_contract
from rescue_ai.infrastructure.local_mission_source import LocalMissionSource
from rescue_ai.infrastructure.pilot_engine import PilotMissionEngineFactory
from rescue_ai.infrastructure.s3_artifact_store import (
    LocalArtifactStorage,
    S3ArtifactBackendSettings,
    S3ArtifactStorage,
)
from rescue_ai.infrastructure.stage_store import LocalStageStore, S3StageStore
from rescue_ai.infrastructure.status_store import JsonStatusStore, PostgresStatusStore
from rescue_ai.infrastructure.yolo_detector import YoloDetector

# ── CLI argument parsing ────────────────────────────────────────


STAGES = ("data", "train", "validate", "inference")


def parse_args() -> argparse.Namespace:
    """Parse unified pipeline CLI arguments."""
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Rescue-AI ML pipeline (data/train/validate/inference)"
    )
    parser.add_argument(
        "--stage", required=True, choices=STAGES, help="Pipeline stage to run"
    )
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--ds", required=True)
    parser.add_argument("--model-version", default=settings.batch.model_version)
    parser.add_argument("--code-version", default=settings.batch.code_version)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--min-accuracy", type=float, default=0.75)
    return parser.parse_args()


# ── Factory functions ───────────────────────────────────────────


def build_stage_store():
    """Build stage artifact store (local or S3) from settings."""
    settings = get_settings()
    if settings.batch.artifact_backend == "s3" and settings.batch.s3.ready:
        return S3StageStore(
            endpoint_url=settings.batch.s3.endpoint,
            region_name=settings.batch.s3.region,
            access_key=settings.batch.s3.access_key_id,
            secret_key=settings.batch.s3.secret_access_key,
            bucket=settings.batch.s3.bucket or "",
        )
    return LocalStageStore(root=settings.batch.artifact_root / "stages")


def build_status_store():
    """Build run status store based on environment configuration."""
    settings = get_settings()
    if settings.batch.status_backend == "postgres":
        dsn = settings.batch.postgres_dsn
        if not dsn:
            raise ValueError("BATCH_POSTGRES_DSN is required for postgres backend")
        return PostgresStatusStore(dsn=dsn)
    return JsonStatusStore(path=settings.batch.status_path)


def build_artifact_store():
    """Build artifact store based on environment configuration."""
    settings = get_settings()
    if settings.batch.artifact_backend == "s3":
        bucket = settings.batch.s3.bucket
        if not bucket:
            raise ValueError("BATCH_S3_BUCKET is required for s3 backend")
        s3_settings = S3ArtifactBackendSettings(
            endpoint=settings.batch.s3.endpoint,
            region=settings.batch.s3.region,
            access_key_id=settings.batch.s3.access_key_id,
            secret_access_key=settings.batch.s3.secret_access_key,
            bucket=bucket,
        )
        fallback = LocalArtifactStorage(root=settings.batch.artifact_root)
        return S3ArtifactStorage(settings=s3_settings, fallback_storage=fallback)
    return LocalArtifactStorage(root=settings.batch.artifact_root)


def build_source() -> LocalMissionSource:
    """Build local mission source for batch processing."""
    settings = get_settings()
    return LocalMissionSource(
        root_dir=settings.batch.mission_root,
        fps=settings.batch.source_fps,
    )


def build_runner(detector) -> MissionBatchRunner:
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


# ── Main ────────────────────────────────────────────────────────


def main() -> None:
    """Run a single pipeline stage."""
    args = parse_args()
    settings = get_settings()

    store = build_stage_store()
    paths = PipelinePaths(
        prefix=settings.batch.s3.prefix,
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

        def _model_probe():
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

        def _runner_factory():
            contract = load_stream_contract()
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

        result = run_inference_stage(
            store, paths, force=args.force, runner_factory=_runner_factory
        )
    else:
        raise ValueError(f"Unknown stage: {args.stage}")

    print_result(result)


if __name__ == "__main__":
    main()
