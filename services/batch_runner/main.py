from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from libs.batch.application import MissionBatchRunner
from libs.batch.domain.models import BatchRunRequest
from libs.batch.infrastructure import (
    FakeDetectionRuntime,
    JsonStatusStore,
    LocalArtifactStore,
    LocalMissionSource,
    PilotMissionEngineFactory,
    PostgresStatusStore,
    S3ArtifactStore,
    YoloDetectionRuntime,
)
from libs.core.application.models import AlertRuleConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Rescue-AI batch mission")
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--ds", required=True)
    parser.add_argument(
        "--model-version",
        default=os.getenv("BATCH_MODEL_VERSION", "yolov8n_baseline_multiscale"),
    )
    parser.add_argument(
        "--code-version", default=os.getenv("BATCH_CODE_VERSION", "dev")
    )
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def build_status_store():
    backend = os.getenv("BATCH_STATUS_BACKEND", _default_status_backend())
    if backend == "postgres":
        dsn = os.getenv("BATCH_POSTGRES_DSN", "")
        if not dsn:
            raise ValueError("BATCH_POSTGRES_DSN is required for postgres backend")
        return PostgresStatusStore(dsn=dsn)
    status_path = Path(
        os.getenv("BATCH_STATUS_PATH", "/opt/airflow/data/status/runs.json")
    )
    return JsonStatusStore(path=status_path)


def build_artifact_store():
    backend = os.getenv("BATCH_ARTIFACT_BACKEND", _default_artifact_backend())
    if backend == "s3":
        bucket = os.getenv("BATCH_S3_BUCKET", "")
        if not bucket:
            raise ValueError("BATCH_S3_BUCKET is required for s3 backend")
        return S3ArtifactStore(
            bucket=bucket,
            prefix=os.getenv("BATCH_S3_PREFIX", "batch"),
            endpoint_url=os.getenv("BATCH_S3_ENDPOINT"),
            access_key=os.getenv("BATCH_S3_ACCESS_KEY"),
            secret_key=os.getenv("BATCH_S3_SECRET_KEY"),
            region_name=os.getenv("BATCH_S3_REGION", "us-east-1"),
        )
    artifact_root = Path(
        os.getenv("BATCH_ARTIFACT_ROOT", "/opt/airflow/data/artifacts")
    )
    return LocalArtifactStore(root_dir=artifact_root)


def build_source():
    source_root = Path(os.getenv("BATCH_MISSION_ROOT", "/opt/airflow/data/missions"))
    source_fps = float(os.getenv("BATCH_SOURCE_FPS", "6.0"))
    return LocalMissionSource(root_dir=source_root, fps=source_fps)


def build_alert_rules(runtime: YoloDetectionRuntime) -> AlertRuleConfig:
    runtime_rules = runtime.rules
    return AlertRuleConfig(
        score_threshold=runtime_rules.score_threshold,
        window_sec=runtime_rules.window_sec,
        quorum_k=runtime_rules.quorum_k,
        cooldown_sec=runtime_rules.cooldown_sec,
        gap_end_sec=runtime_rules.gap_end_sec,
        gt_gap_end_sec=runtime_rules.gt_gap_end_sec,
        match_tolerance_sec=runtime_rules.match_tolerance_sec,
    )


def build_detector(model_version: str):
    backend = os.getenv("BATCH_DETECTOR_BACKEND", "yolo").lower()
    if backend == "fake":
        return FakeDetectionRuntime(model_version=model_version)
    return YoloDetectionRuntime(model_version=model_version)


def main() -> None:
    args = parse_args()
    detector = build_detector(model_version=args.model_version)
    runner = MissionBatchRunner(
        source=build_source(),
        detector=detector,
        artifacts=build_artifact_store(),
        statuses=build_status_store(),
        engine_factory=PilotMissionEngineFactory(),
    )
    request = BatchRunRequest(
        mission_id=args.mission_id,
        ds=args.ds,
        config_hash=detector.config_hash,
        model_version=args.model_version,
        code_version=args.code_version,
        alert_rules=build_alert_rules(detector),
        force=args.force,
    )
    result = runner.run(request)
    print(
        json.dumps(
            {
                "run_key": result.run_key,
                "status": result.status,
                "report_uri": result.report_uri,
                "debug_uri": result.debug_uri,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()


def _default_status_backend() -> str:
    runtime_env = os.getenv("BATCH_RUNTIME_ENV", "local").lower()
    if runtime_env in {"shared", "stage", "staging", "prod", "production"}:
        return "postgres"
    return "json"


def _default_artifact_backend() -> str:
    runtime_env = os.getenv("BATCH_RUNTIME_ENV", "local").lower()
    if runtime_env in {"shared", "stage", "staging", "prod", "production"}:
        return "s3"
    return "local"
