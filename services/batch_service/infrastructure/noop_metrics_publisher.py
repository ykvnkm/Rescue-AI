from services.batch_service.domain.entities import BatchResult


class NoopMetricsPublisher:
    """Scaffold adapter, replaced later by Prometheus push/export adapter."""

    def publish_batch_result(self, result: BatchResult) -> None:
        _ = result

