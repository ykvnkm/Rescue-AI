from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from services.batch_service.application.contracts import (
    ArtifactStore,
    BatchExecutor,
    JobRepository,
    MetricsPublisher,
)
from services.batch_service.domain.entities import BatchJob, BatchResult


class RunBatchJob:
    """Application use case for one idempotent batch inference job."""

    def __init__(
        self,
        job_repository: JobRepository,
        batch_executor: BatchExecutor,
        artifact_store: ArtifactStore,
        metrics_publisher: MetricsPublisher,
    ) -> None:
        self._job_repository = job_repository
        self._batch_executor = batch_executor
        self._artifact_store = artifact_store
        self._metrics_publisher = metrics_publisher

    def execute(
        self,
        mission_id: str,
        source_uri: str,
        idempotency_key: str | None = None,
    ) -> BatchResult:
        job = BatchJob(
            job_id=str(uuid4()),
            mission_id=mission_id,
            source_uri=source_uri,
            created_at=datetime.now(timezone.utc),
            status="created",
            idempotency_key=idempotency_key,
        )
        self._job_repository.create(job)
        self._job_repository.update_status(job.job_id, "running")

        result = self._batch_executor.run(job)
        self._job_repository.update_status(job.job_id, result.status)

        if result.report_uri:
            self._artifact_store.upload_file(
                local_path=result.report_uri,
                remote_key=f"missions/{mission_id}/reports/{job.job_id}.json",
            )
        if result.metrics_uri:
            self._artifact_store.upload_file(
                local_path=result.metrics_uri,
                remote_key=f"missions/{mission_id}/metrics/{job.job_id}.json",
            )

        self._metrics_publisher.publish_batch_result(result)
        return result

