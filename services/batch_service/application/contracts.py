from typing import Protocol

from services.batch_service.domain.entities import BatchJob, BatchResult


class JobRepository(Protocol):
    """Persistence contract for batch jobs."""

    def create(self, job: BatchJob) -> None: ...
    def get(self, job_id: str) -> BatchJob | None: ...
    def update_status(self, job_id: str, status: str) -> None: ...


class BatchExecutor(Protocol):
    """Port for actual batch execution runtime."""

    def run(self, job: BatchJob) -> BatchResult: ...


class ArtifactStore(Protocol):
    """Port for remote artifact storage (S3-compatible)."""

    def upload_file(self, local_path: str, remote_key: str) -> str: ...


class MetricsPublisher(Protocol):
    """Port for publishing operational metrics for Prometheus scraping."""

    def publish_batch_result(self, result: BatchResult) -> None: ...

