from dataclasses import dataclass
from datetime import datetime
from typing import Literal

BatchJobStatus = Literal["created", "running", "succeeded", "failed"]


@dataclass(frozen=True)
class BatchJob:
    """Domain entity representing one idempotent batch run."""

    job_id: str
    mission_id: str
    source_uri: str
    created_at: datetime
    status: BatchJobStatus = "created"
    idempotency_key: str | None = None


@dataclass(frozen=True)
class BatchResult:
    """Batch execution result stored and published by orchestrators."""

    job_id: str
    status: BatchJobStatus
    processed_frames: int
    alerts_total: int
    report_uri: str | None = None
    metrics_uri: str | None = None
    error_message: str | None = None

