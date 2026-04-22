"""Tests for :class:`AutoSessionManager` session runtime."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pytest

from rescue_ai.application.auto_mission_service import AutoMissionService
from rescue_ai.application.auto_session_manager import (
    AutoSessionManager,
    StartSessionRequest,
)
from rescue_ai.domain.entities import Detection, TrajectoryPoint
from rescue_ai.domain.value_objects import AlertRuleConfig, NavMode, TrajectorySource
from tests.support.in_memory_repositories import (
    InMemoryAlertRepository,
    InMemoryArtifactStorage,
    InMemoryAutoDecisionRepository,
    InMemoryAutoMissionConfigRepository,
    InMemoryDatabase,
    InMemoryFrameEventRepository,
    InMemoryMissionRepository,
    InMemoryTrajectoryRepository,
)


@dataclass
class _FakeDetector:
    queue: list[list[Detection]] = field(default_factory=list)

    def detect(self, image_uri: object) -> list[Detection]:
        _ = image_uri
        if not self.queue:
            return []
        return list(self.queue.pop(0))

    def warmup(self) -> None:
        return None

    def runtime_name(self) -> str:
        return "fake"


@dataclass
class _FakeNavigation:
    reset_calls: int = 0

    def reset(self) -> None:
        self.reset_calls += 1

    def step(
        self,
        frame_bgr: object,
        ts_sec: float,
        frame_id: int | None = None,
    ) -> TrajectoryPoint | None:
        _ = frame_bgr
        if frame_id is None:
            return None
        return TrajectoryPoint(
            mission_id="nav",
            seq=0,
            ts_sec=ts_sec,
            frame_id=frame_id,
            x=float(frame_id),
            y=float(frame_id),
            z=0.5,
            source=TrajectorySource.OPTICAL_FLOW,
        )


class _FakeVideoSource:
    """VideoFramePort double producing a fixed list of frames.

    If ``block_after`` is set, yields that many frames and then blocks on
    ``_released`` — allowing tests to observe a long-running session.
    """

    def __init__(
        self,
        *,
        frame_count: int = 3,
        block_after: int | None = None,
    ) -> None:
        self._frames = [
            (np.zeros((4, 8, 3), dtype=np.uint8), i * 0.5, i)
            for i in range(frame_count)
        ]
        self._closed = False
        self._block_after = block_after
        self._released = threading.Event()

    def frames(self):
        for idx, frame in enumerate(self._frames):
            if self._block_after is not None and idx == self._block_after:
                self._released.wait(timeout=5.0)
            yield frame

    def release(self) -> None:
        self._released.set()

    def close(self) -> None:
        self._closed = True
        self._released.set()

    @property
    def closed(self) -> bool:
        return self._closed


def _alert_rules() -> AlertRuleConfig:
    return AlertRuleConfig(
        score_threshold=0.5,
        window_sec=2.0,
        quorum_k=1,
        cooldown_sec=10.0,
        gap_end_sec=5.0,
        gt_gap_end_sec=5.0,
        match_tolerance_sec=1.0,
    )


def _build_service() -> tuple[AutoMissionService, _FakeDetector, _FakeNavigation]:
    db = InMemoryDatabase()
    detector = _FakeDetector()
    navigation = _FakeNavigation()
    service = AutoMissionService(
        dependencies=AutoMissionService.Dependencies(
            mission_repository=InMemoryMissionRepository(db),
            alert_repository=InMemoryAlertRepository(db),
            frame_event_repository=InMemoryFrameEventRepository(db),
            trajectory_repository=InMemoryTrajectoryRepository(),
            auto_decision_repository=InMemoryAutoDecisionRepository(),
            auto_mission_config_repository=InMemoryAutoMissionConfigRepository(),
            artifact_storage=InMemoryArtifactStorage(),
            detector=detector,
            navigation_engine=navigation,
            trajectory_plot_renderer=None,
        ),
        alert_rules=_alert_rules(),
    )
    return service, detector, navigation


def _collect_events(session, events: list[Any]) -> None:
    def _listener(event: Any) -> None:
        events.append(event)

    session.subscribe(_listener)


def test_start_session_streams_frames_and_completes() -> None:
    service, _detector, _navigation = _build_service()
    manager = AutoSessionManager(service=service, ws_max_width=0, ws_emit_max_fps=0.0)
    events: list[dict[str, Any]] = []

    source = _FakeVideoSource(frame_count=3)
    session = manager.start_session(
        request=StartSessionRequest(
            source=source,
            source_kind="video",
            source_value="test://fake.mp4",
            nav_mode=NavMode.AUTO,
            detector_name="yolo",
            fps=2.0,
        ),
    )
    _collect_events(session, events)

    assert session.wait_done(timeout=5.0) is True
    session.join(timeout=1.0)

    assert source.closed is True
    assert manager.get_active() is None

    types = [evt["type"] for evt in events]
    # "ready" is emitted before we subscribe here, so the listener gets
    # only frame + done events.
    assert "frame" in types
    assert types[-1] == "done"

    frame_events = [evt for evt in events if evt["type"] == "frame"]
    assert len(frame_events) >= 1
    for evt in frame_events:
        assert evt["session_id"] == session.session_id
        assert evt["mission_id"] == session.mission.mission_id
        assert evt["frame_jpeg_b64"] is not None
        assert evt["trajectory_point"] is not None

    done_event = events[-1]
    assert done_event["frames_consumed"] == 3
    assert done_event["frames_emitted"] >= 1


def test_start_session_rejects_concurrent_sessions() -> None:
    service, _detector, _navigation = _build_service()
    manager = AutoSessionManager(service=service, ws_max_width=0, ws_emit_max_fps=0.0)
    source_a = _FakeVideoSource(frame_count=5, block_after=0)
    session_a = manager.start_session(
        request=StartSessionRequest(
            source=source_a,
            source_kind="video",
            source_value="a",
            nav_mode=NavMode.AUTO,
            detector_name="yolo",
            fps=2.0,
        ),
    )
    try:
        with pytest.raises(RuntimeError, match="already running"):
            manager.start_session(
                request=StartSessionRequest(
                    source=_FakeVideoSource(frame_count=1),
                    source_kind="video",
                    source_value="b",
                    nav_mode=NavMode.AUTO,
                    detector_name="yolo",
                    fps=2.0,
                ),
            )
    finally:
        source_a.release()
        session_a.wait_done(timeout=5.0)
        session_a.join(timeout=1.0)


def test_stop_session_drains_thread() -> None:
    service, _detector, _navigation = _build_service()
    manager = AutoSessionManager(service=service, ws_max_width=0, ws_emit_max_fps=0.0)
    source = _FakeVideoSource(frame_count=100, block_after=1)
    session = manager.start_session(
        request=StartSessionRequest(
            source=source,
            source_kind="video",
            source_value="long",
            nav_mode=NavMode.AUTO,
            detector_name="yolo",
            fps=2.0,
        ),
    )
    # Let the loop process one frame and then block inside the source.
    time.sleep(0.05)

    stopped = manager.stop_session(session.session_id, timeout=5.0)
    assert stopped is session
    assert manager.get_active() is None
    assert source.closed is True


def test_require_unknown_session_raises() -> None:
    service, _detector, _navigation = _build_service()
    manager = AutoSessionManager(service=service)
    with pytest.raises(LookupError):
        manager.require("missing")
