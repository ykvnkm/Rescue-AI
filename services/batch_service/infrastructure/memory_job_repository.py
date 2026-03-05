from typing import cast

from services.batch_service.domain.entities import BatchJob, BatchJobStatus


class InMemoryJobRepository:
    """Simple repository stub for local development and tests."""

    def __init__(self) -> None:
        self._jobs: dict[str, BatchJob] = {}

    def create(self, job: BatchJob) -> None:
        self._jobs[job.job_id] = job

    def get(self, job_id: str) -> BatchJob | None:
        return self._jobs.get(job_id)

    def update_status(self, job_id: str, status: str) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        typed_status: BatchJobStatus = (
            cast(BatchJobStatus, status) if status in _ALLOWED_STATUSES else "failed"
        )
        self._jobs[job_id] = BatchJob(
            job_id=job.job_id,
            mission_id=job.mission_id,
            source_uri=job.source_uri,
            created_at=job.created_at,
            status=typed_status,
            idempotency_key=job.idempotency_key,
        )


_ALLOWED_STATUSES: set[BatchJobStatus] = {
    "created",
    "running",
    "succeeded",
    "failed",
}
