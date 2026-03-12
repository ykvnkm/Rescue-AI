from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from libs.core.application.models import AlertRuleConfig

# pylint: disable=too-few-public-methods,missing-class-docstring


@dataclass(frozen=True)
class BatchRunRequest:
    mission_id: str
    ds: str
    config_hash: str
    model_version: str
    code_version: str
    alert_rules: AlertRuleConfig
    force: bool = False

    @property
    def run_key(self) -> str:
        return f"{self.mission_id}:{self.ds}:{self.config_hash}:{self.model_version}"


@dataclass(frozen=True)
class FrameRecord:
    frame_id: int
    ts_sec: float
    frame_path: Path
    image_uri: str
    gt_person_present: bool
    is_corrupted: bool = False


@dataclass(frozen=True)
class MissionInput:
    source_uri: str
    frames: list[FrameRecord]
    gt_available: bool


@dataclass(frozen=True)
class RunStatusRecord:
    run_key: str
    status: str
    reason: str | None = None
    report_uri: str | None = None
    debug_uri: str | None = None


@dataclass
class DataQuality:
    total_frames: int = 0
    processed_frames: int = 0
    corrupted_frames: int = 0
    detector_error_frames: int = 0
    missing_gt_frames: int = 0

    def as_dict(self) -> dict[str, object]:
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


@dataclass(frozen=True)
class BatchRunResult:
    run_key: str
    status: str
    report_uri: str | None
    debug_uri: str | None
    report: dict[str, object] = field(default_factory=dict)
