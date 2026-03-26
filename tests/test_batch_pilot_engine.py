from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from rescue_ai.domain.entities import Detection, FrameEvent
from rescue_ai.infrastructure.pilot_engine import PilotMissionEngine, PilotServicePort


@dataclass
class _Mission:
    mission_id: str


class _PilotStub:
    def __init__(self) -> None:
        self.metadata: dict[str, object] = {}

    def set_report_metadata(self, payload: dict[str, object]) -> None:
        self.metadata = payload

    def create_mission(self, source_name: str, total_frames: int, fps: float):
        _ = (source_name, total_frames, fps)
        return _Mission("m1")

    def start_mission(self, mission_id: str):
        _ = mission_id
        return _Mission("m1")

    def ingest_frame_event(self, frame_event: FrameEvent, detections: list[Detection]):
        _ = (frame_event, detections)
        return []

    def review_alert(self, alert_id: str, updates: dict[str, object]):
        """Stub review_alert that accepts a dict."""
        _ = (alert_id, updates)
        return object()

    def complete_mission(self, mission_id: str, completed_frame_id: int | None):
        _ = (mission_id, completed_frame_id)
        return _Mission("m1")

    def get_mission_report(self, mission_id: str) -> dict[str, object]:
        _ = mission_id
        return {"status": "completed"}


def test_pilot_mission_engine_happy_path() -> None:
    pilot = _PilotStub()
    engine = PilotMissionEngine(pilot=cast(PilotServicePort, pilot))

    mission_id = engine.create_and_start_mission(
        source_name="source",
        total_frames=1,
        fps=1.0,
        report_metadata={"a": 1},
    )
    engine.ingest_frame(
        mission_id=mission_id,
        frame_event=FrameEvent(
            mission_id=mission_id,
            frame_id=1,
            ts_sec=0.0,
            image_uri="/tmp/frame.jpg",
            gt_person_present=False,
            gt_episode_id=None,
        ),
        detections=[],
    )
    engine.complete(mission_id, completed_frame_id=1)
    report = engine.build_report(mission_id)

    assert mission_id == "m1"
    assert report["status"] == "completed"
    assert pilot.metadata == {"a": 1}
