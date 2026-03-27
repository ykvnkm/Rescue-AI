"""Batch processing DTOs and port protocols.

This module contains the data transfer objects and port definitions
used by both the batch runner (application) and its adapters (infrastructure).
Separated from batch_runner.py to keep infrastructure dependencies on
thin contracts rather than use-case orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from rescue_ai.domain.entities import Alert, Detection, FrameEvent
from rescue_ai.domain.ports import ReportMetadataPayload
from rescue_ai.domain.value_objects import AlertRuleConfig, AlertStatus

# ── Data transfer objects ───────────────────────────────────────


@dataclass(frozen=True)
class FrameRecord:
    """Single frame metadata used by batch processing."""

    frame_id: int
    ts_sec: float
    frame_path: Path
    image_uri: str
    gt_person_present: bool
    is_corrupted: bool = False


@dataclass(frozen=True)
class MissionInput:
    """Resolved mission source for a concrete processing date."""

    source_uri: str
    frames: list[FrameRecord]
    gt_available: bool


@dataclass(frozen=True)
class RunStatusRecord:
    """Persisted status snapshot for a batch run key."""

    run_key: str
    status: str
    reason: str | None = None
    report_uri: str | None = None
    debug_uri: str | None = None


@dataclass(frozen=True)
class BatchRunRequest:
    """Input command for one batch mission run."""

    mission_id: str
    ds: str
    config_hash: str
    model_version: str
    code_version: str
    alert_rules: AlertRuleConfig
    force: bool = False

    @property
    def run_key(self) -> str:
        """Unique idempotency key for this run."""
        return f"{self.mission_id}:{self.ds}:{self.config_hash}:{self.model_version}"


@dataclass(frozen=True)
class BatchRunResult:
    """Output summary for a completed batch runner execution."""

    run_key: str
    status: str
    report_uri: str | None
    debug_uri: str | None
    report: dict[str, object] = field(default_factory=dict)


@dataclass
class DataQuality:
    """Frame-level quality counters used for final run status."""

    total_frames: int = 0
    processed_frames: int = 0
    corrupted_frames: int = 0
    detector_error_frames: int = 0
    missing_gt_frames: int = 0

    def as_dict(self) -> dict[str, object]:
        """Return quality metrics as a plain dictionary."""
        invalid_frames = self.corrupted_frames + self.detector_error_frames
        error_rate = (
            invalid_frames / self.total_frames if self.total_frames > 0 else 0.0
        )
        return {
            "total_frames": self.total_frames,
            "processed_frames": self.processed_frames,
            "corrupted_frames": self.corrupted_frames,
            "detector_error_frames": self.detector_error_frames,
            "missing_gt_frames": self.missing_gt_frames,
            "error_rate": round(error_rate, 4),
            "input_empty": self.total_frames == 0,
        }


# ── Batch-specific port protocols ───────────────────────────────


class BatchArtifactPort(Protocol):
    """Artifact storage contract for batch pipeline outputs."""

    def write_report(self, run_key: str, payload: dict[str, object]) -> str: ...

    def write_debug_rows(self, run_key: str, rows: list[dict[str, object]]) -> str: ...


class MissionSourcePort(Protocol):
    """Loads mission input frames and optional annotations for a given date."""

    def load(self, mission_id: str, ds: str) -> MissionInput:
        """Load mission input frames for the given mission and date."""

    def describe_source(self) -> str:
        """Return a human-readable description of this mission source."""


class RunStatusStorePort(Protocol):
    """Stores and retrieves batch run status records."""

    def get(self, run_key: str) -> RunStatusRecord | None:
        """Retrieve the status record for the given run key."""

    def upsert(self, record: RunStatusRecord) -> None:
        """Insert or update a run status record."""


class MissionEnginePort(Protocol):
    """Mission lifecycle API used by the batch runner."""

    def create_and_start_mission(
        self,
        source_name: str,
        total_frames: int,
        fps: float,
        report_metadata: ReportMetadataPayload,
    ) -> str:
        """Create a new mission and start processing."""

    def ingest_frame(
        self,
        mission_id: str,
        frame_event: FrameEvent,
        detections: list[Detection],
    ) -> list[Alert]:
        """Ingest a single frame with its detections and return any alerts."""

    def review_alert(
        self,
        alert_id: str,
        status: AlertStatus,
        reviewed_at_sec: float,
        reason: str,
    ) -> None:
        """Record a review decision for the given alert."""

    def complete(self, mission_id: str, completed_frame_id: int | None) -> None:
        """Mark the mission as complete."""

    def build_report(self, mission_id: str) -> dict[str, object]:
        """Build and return the final mission report."""


class MissionEngineFactoryPort(Protocol):
    """Builds mission engine instances for a single batch run."""

    def create(
        self,
        alert_rules: AlertRuleConfig,
        report_metadata: ReportMetadataPayload,
    ) -> MissionEnginePort:
        """Create a new mission engine instance for a batch run."""

    def factory_name(self) -> str:
        """Return a human-readable name for this factory."""
