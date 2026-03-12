from __future__ import annotations

from typing import Protocol

from libs.batch.domain.models import MissionInput, RunStatusRecord
from libs.core.application.models import AlertRuleConfig, DetectionInput
from libs.core.domain.entities import Alert, FrameEvent

# pylint: disable=too-few-public-methods,missing-class-docstring
# pylint: disable=too-many-arguments,too-many-positional-arguments


class MissionSourcePort(Protocol):
    def load(self, mission_id: str, ds: str) -> MissionInput: ...


class DetectionRuntimePort(Protocol):
    def detect(self, image_uri: str) -> list[DetectionInput]: ...


class ArtifactStorePort(Protocol):
    def write_report(self, run_key: str, payload: dict[str, object]) -> str: ...

    def write_debug_rows(
        self,
        run_key: str,
        rows: list[dict[str, object]],
    ) -> str: ...


class RunStatusStorePort(Protocol):
    def get(self, run_key: str) -> RunStatusRecord | None: ...

    def upsert(
        self,
        run_key: str,
        status: str,
        reason: str | None = None,
        report_uri: str | None = None,
        debug_uri: str | None = None,
    ) -> None: ...


class MissionEnginePort(Protocol):
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
    def create(
        self,
        alert_rules: AlertRuleConfig,
        report_metadata: dict[str, object],
    ) -> MissionEnginePort: ...
