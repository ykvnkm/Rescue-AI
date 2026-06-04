"""Sync-worker drains the replication outbox into remote stores.

ADR-0007 §3. The worker is intentionally I/O-thin: it claims a batch
of pending rows, calls the configured `RemoteSyncTarget`, and updates
the row status. Idempotency is delegated to the remote target — DB
upserts use ``idempotency_key``, S3 uploads write to a deterministic
key — so a double delivery never produces a duplicate.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from rescue_ai.domain.ports import (
    OutboxRow,
    RemoteSyncTarget,
    RemoteUnavailableError,
    SyncOutbox,
)

logger = logging.getLogger(__name__)
SYNC_WORKER_RECOVERABLE_ERRORS = (
    RuntimeError,
    OSError,
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
)


@dataclass(frozen=True)
class SyncWorkerConfig:
    """Tuning knobs for one sync-worker process."""

    batch_size: int = 50
    interval_sec: float = 10.0
    max_attempts: int = 10
    processing_timeout_sec: float = 120.0


class SyncWorker:
    """Drains the outbox at-least-once.

    The class is sync-friendly (no asyncio); it is meant to run as a
    standalone process via :mod:`run_worker`.
    """

    def __init__(
        self,
        outbox: SyncOutbox,
        target: RemoteSyncTarget,
        config: SyncWorkerConfig,
    ) -> None:
        self._outbox = outbox
        self._target = target
        self._config = config

    def run_once(self) -> int:
        """Process one batch and return how many rows were synced.

        On a connectivity failure (``RemoteUnavailableError``) the batch is
        aborted immediately: the already-claimed rows keep their
        ``processing`` state and are returned to ``pending`` by the next
        ``reset_stuck`` sweep, so no delivery attempt is consumed while the
        uplink is down (ADR-0007 §3 offline-first guarantee).
        """
        self._outbox.reset_stuck(self._config.processing_timeout_sec)
        rows = self._outbox.claim_pending(self._config.batch_size)
        synced = 0
        for index, row in enumerate(rows):
            try:
                if self._handle_row(row):
                    synced += 1
            except RemoteUnavailableError as error:
                logger.warning(
                    "sync-worker: remote unavailable (%s); aborting batch, "
                    "%d row(s) deferred for retry (no attempt consumed)",
                    error,
                    len(rows) - index,
                )
                break
        return synced

    def run_forever(self, *, sleep: Callable[[float], None] | None = None) -> None:
        sleep_fn = sleep or time.sleep
        while True:
            try:
                self.run_once()
            except SYNC_WORKER_RECOVERABLE_ERRORS:  # pragma: no cover
                # Top-level safety net: a station must keep draining even if a
                # cycle raises unexpectedly (local DB blip, bug). Log and retry.
                logger.exception("sync-worker iteration failed")
            sleep_fn(self._config.interval_sec)

    def _handle_row(self, row: OutboxRow) -> bool:
        if row.attempts >= self._config.max_attempts:
            logger.warning(
                "outbox row %s exceeded max_attempts=%s, leaving for ops",
                row.id,
                self._config.max_attempts,
            )
            self._outbox.mark_failed(
                row.id, f"max_attempts={self._config.max_attempts}"
            )
            return False
        try:
            self._target.deliver(row)
        except SYNC_WORKER_RECOVERABLE_ERRORS as error:
            logger.warning(
                "outbox row %s delivery failed: %s",
                row.id,
                error,
            )
            self._outbox.mark_failed(row.id, str(error))
            return False
        self._outbox.mark_synced(row.id)
        return True
