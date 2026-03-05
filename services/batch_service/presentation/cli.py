from __future__ import annotations

import argparse

from services.batch_service.application.use_cases import RunBatchJob
from services.batch_service.infrastructure.local_batch_executor import LocalBatchExecutor
from services.batch_service.infrastructure.memory_job_repository import (
    InMemoryJobRepository,
)
from services.batch_service.infrastructure.noop_metrics_publisher import (
    NoopMetricsPublisher,
)
from services.batch_service.infrastructure.s3_artifact_store import S3ArtifactStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one batch job")
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--source-uri", required=True)
    parser.add_argument("--idempotency-key", default=None)
    parser.add_argument("--s3-endpoint", default="http://minio:9000")
    parser.add_argument("--s3-bucket", default="rescue-ai-artifacts")
    parser.add_argument("--s3-access-key", default="minio")
    parser.add_argument("--s3-secret-key", default="minio123")
    args = parser.parse_args()

    use_case = RunBatchJob(
        job_repository=InMemoryJobRepository(),
        batch_executor=LocalBatchExecutor(),
        artifact_store=S3ArtifactStore(
            endpoint_url=args.s3_endpoint,
            bucket=args.s3_bucket,
            access_key=args.s3_access_key,
            secret_key=args.s3_secret_key,
        ),
        metrics_publisher=NoopMetricsPublisher(),
    )
    result = use_case.execute(
        mission_id=args.mission_id,
        source_uri=args.source_uri,
        idempotency_key=args.idempotency_key,
    )
    print(
        {
            "job_id": result.job_id,
            "status": result.status,
            "processed_frames": result.processed_frames,
            "alerts_total": result.alerts_total,
            "report_uri": result.report_uri,
            "metrics_uri": result.metrics_uri,
        }
    )


if __name__ == "__main__":
    main()

