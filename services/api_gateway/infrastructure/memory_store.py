"""Backward-compatible import surface for shared in-memory repositories."""

from libs.infra import memory as _memory

InMemoryAlertRepository = _memory.InMemoryAlertRepository
InMemoryDatabase = _memory.InMemoryDatabase
InMemoryFrameEventRepository = _memory.InMemoryFrameEventRepository
InMemoryMissionRepository = _memory.InMemoryMissionRepository

__all__ = (
    "InMemoryAlertRepository",
    "InMemoryDatabase",
    "InMemoryFrameEventRepository",
    "InMemoryMissionRepository",
)
