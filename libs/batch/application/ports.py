from __future__ import annotations

from typing import Protocol

from libs.batch.domain.models import MissionInput, RunStatusRecord
from libs.core.application.models import AlertRuleConfig, DetectionInput
from libs.core.domain.entities import Alert, FrameEvent


class MissionSourcePort(Protocol):
    """Loads mission input frames and optional annotations for a given date."""

    def load(self, mission_id: str, ds: str) -> MissionInput: ...

    def describe_source(self) -> str: ...


class DetectionRuntimePort(Protocol):
    """Runs detection on a single frame and returns normalized detections."""

    def detect(self, image_uri: str) -> list[DetectionInput]: ...

    def runtime_name(self) -> str: ...


class ArtifactStorePort(Protocol):
    """Persists batch artifacts (report and frame-level debug output)."""

    def write_report(self, run_key: str, payload: dict[str, object]) -> str: ...

    def write_debug_rows(
        self,
        run_key: str,
        rows: list[dict[str, object]],
    ) -> str: ...


class RunStatusStorePort(Protocol):
    """Stores and retrieves batch run status records."""

    def get(self, run_key: str) -> RunStatusRecord | None: ...

    def upsert(self, record: RunStatusRecord) -> None: ...


class MissionEnginePort(Protocol):
    """Mission lifecycle API used by the batch runner."""

    def create_and_start_mission(
        self,
        source_name: str,
        total_frames: int,
        fps: float,
        report_metadata: dict[str, object],
    ) -> str: ...

    def ingest_frame(
        self,
        mission_id: str,
        frame_event: FrameEvent,
        detections: list[DetectionInput],
    ) -> list[Alert]: ...

    def review_alert(
        self,
        alert_id: str,
        status: str,
        reviewed_at_sec: float,
        reason: str,
    ) -> None: ...

    def complete(self, mission_id: str, completed_frame_id: int | None) -> None: ...

    def build_report(self, mission_id: str) -> dict[str, object]: ...


class MissionEngineFactoryPort(Protocol):
    """Builds mission engine instances for a single batch run."""

    def create(
        self,
        alert_rules: AlertRuleConfig,
        report_metadata: dict[str, object],
    ) -> MissionEnginePort: ...

    def factory_name(self) -> str: ...
