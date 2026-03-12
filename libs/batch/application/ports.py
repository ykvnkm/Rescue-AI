from __future__ import annotations

from typing import Protocol

from libs.batch.domain.models import MissionInput, RunStatusRecord
from libs.core.application.models import DetectionInput

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
