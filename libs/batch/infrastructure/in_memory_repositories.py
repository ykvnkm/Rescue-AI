from dataclasses import dataclass

from libs.core.domain.entities import FrameEvent
from libs.infra.memory.repositories import (
    InMemoryAlertRepository,
    InMemoryDatabase,
    InMemoryFrameEventRepository,
    InMemoryMissionRepository,
)


@dataclass
class InMemoryBatchDb(InMemoryDatabase):
    """Batch alias over shared in-memory database."""

    @property
    def frames(self) -> dict[str, list[FrameEvent]]:
        """Backward-compatible frame storage alias."""
        return self.mission_frames


class InMemoryMissionRepo(InMemoryMissionRepository):
    """Batch mission repository backed by shared in-memory implementation."""


class InMemoryAlertRepo(InMemoryAlertRepository):
    """Batch alert repository backed by shared in-memory implementation."""


class InMemoryFrameEventRepo(InMemoryFrameEventRepository):
    """Batch frame repository backed by shared in-memory implementation."""
