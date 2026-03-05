from pathlib import Path

from services.batch_service.domain.entities import BatchJob, BatchResult


class LocalBatchExecutor:
    """Placeholder adapter for running local/offline batch jobs."""

    def __init__(self, runtime_dir: str = "runtime/batch") -> None:
        self._runtime_dir = Path(runtime_dir)
        self._runtime_dir.mkdir(parents=True, exist_ok=True)

    def run(self, job: BatchJob) -> BatchResult:
        report_path = self._runtime_dir / f"{job.job_id}_report.json"
        metrics_path = self._runtime_dir / f"{job.job_id}_metrics.json"
        report_path.write_text("{}", encoding="utf-8")
        metrics_path.write_text("{}", encoding="utf-8")
        return BatchResult(
            job_id=job.job_id,
            status="succeeded",
            processed_frames=0,
            alerts_total=0,
            report_uri=str(report_path),
            metrics_uri=str(metrics_path),
        )

