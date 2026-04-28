"""Tests for :class:`AutoMissionService` (ADR-0006 automatic mode)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from rescue_ai.application.auto_mission_service import (
    AutoFrameOutcome,
    AutoMissionService,
)
from rescue_ai.domain.entities import Alert, Detection, FrameEvent, TrajectoryPoint
from rescue_ai.domain.value_objects import (
    AlertRuleConfig,
    AutoDecisionKind,
    MissionMode,
    NavMode,
    TrajectorySource,
)
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
class FakeDetector:
    """Test double for detector port with queued frame outputs."""

    queue: list[list[Detection]] = field(default_factory=list)
    warmup_called: int = 0

    def detect(self, image_uri: object) -> list[Detection]:
        _ = image_uri
        if not self.queue:
            return []
        return list(self.queue.pop(0))

    def warmup(self) -> None:
        self.warmup_called += 1

    def runtime_name(self) -> str:
        return "fake-detector"


@dataclass
class FakeNavigationEngine:
    """Test double for navigation engine with queued trajectory points."""

    queue: list[TrajectoryPoint | None] = field(default_factory=list)
    reset_calls: int = 0
    last_nav_mode: NavMode | None = None
    last_fps: float | None = None

    def reset(
        self,
        *,
        nav_mode: NavMode | None = None,
        fps: float | None = None,
    ) -> None:
        self.reset_calls += 1
        self.last_nav_mode = nav_mode
        self.last_fps = fps

    def step(
        self,
        frame_bgr: object,
        ts_sec: float,
        frame_id: int | None = None,
    ) -> TrajectoryPoint | None:
        _ = frame_bgr
        if not self.queue:
            return None
        point = self.queue.pop(0)
        if point is None:
            return None
        return TrajectoryPoint(
            mission_id="navigation-origin",
            seq=0,
            ts_sec=ts_sec,
            frame_id=frame_id,
            x=point.x,
            y=point.y,
            z=point.z,
            source=point.source,
        )


class RecordingFrameEventRepository(InMemoryFrameEventRepository):
    def __init__(self, db: InMemoryDatabase, calls: list[str]) -> None:
        super().__init__(db)
        self._calls = calls

    def add(self, frame_event: FrameEvent) -> None:
        self._calls.append(f"frame:{frame_event.frame_id}")
        super().add(frame_event)


class ForeignKeyCheckingAlertRepository(InMemoryAlertRepository):
    def __init__(self, db: InMemoryDatabase, calls: list[str]) -> None:
        super().__init__(db)
        self._db = db
        self._calls = calls

    def add(self, alert: Alert) -> None:
        exists = any(
            frame.frame_id == alert.frame_id
            for frame in self._db.mission_frames.get(alert.mission_id, [])
        )
        if not exists:
            raise AssertionError("alert inserted before frame event")
        self._calls.append(f"alert:{alert.frame_id}")
        super().add(alert)


def _alert_rules(**overrides: float) -> AlertRuleConfig:
    base: dict[str, Any] = {
        "score_threshold": 0.5,
        "window_sec": 2.0,
        "quorum_k": 1,
        "cooldown_sec": 10.0,
        "gap_end_sec": 5.0,
        "gt_gap_end_sec": 5.0,
        "match_tolerance_sec": 1.0,
    }
    base.update(overrides)
    return AlertRuleConfig(
        score_threshold=float(base["score_threshold"]),
        window_sec=float(base["window_sec"]),
        quorum_k=int(base["quorum_k"]),
        cooldown_sec=float(base["cooldown_sec"]),
        gap_end_sec=float(base["gap_end_sec"]),
        gt_gap_end_sec=float(base["gt_gap_end_sec"]),
        match_tolerance_sec=float(base["match_tolerance_sec"]),
    )


@dataclass
class ServiceHarness:
    """Bundled in-memory collaborators for service-level tests."""

    service: AutoMissionService
    db: InMemoryDatabase
    trajectory_repo: InMemoryTrajectoryRepository
    decision_repo: InMemoryAutoDecisionRepository
    config_repo: InMemoryAutoMissionConfigRepository
    artifacts: InMemoryArtifactStorage
    detector: FakeDetector
    navigation: FakeNavigationEngine


class FakeTrajectoryPlotRenderer:
    """Test plot renderer that records call metadata."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def render(self, mission_id: str, points) -> bytes:
        self.calls.append((mission_id, len(list(points))))
        return b"PNG-BYTES"


def _build_service(
    *,
    alert_rules: AlertRuleConfig | None = None,
    detector: FakeDetector | None = None,
    navigation: FakeNavigationEngine | None = None,
    plot_renderer: FakeTrajectoryPlotRenderer | None = None,
) -> ServiceHarness:
    db = InMemoryDatabase()
    trajectory_repo = InMemoryTrajectoryRepository()
    decision_repo = InMemoryAutoDecisionRepository()
    config_repo = InMemoryAutoMissionConfigRepository()
    artifacts = InMemoryArtifactStorage()
    detector = detector or FakeDetector()
    navigation = navigation or FakeNavigationEngine()

    service = AutoMissionService(
        dependencies=AutoMissionService.Dependencies(
            mission_repository=InMemoryMissionRepository(db),
            alert_repository=InMemoryAlertRepository(db),
            frame_event_repository=InMemoryFrameEventRepository(db),
            trajectory_repository=trajectory_repo,
            auto_decision_repository=decision_repo,
            auto_mission_config_repository=config_repo,
            artifact_storage=artifacts,
            detector=detector,
            navigation_engine=navigation,
            trajectory_plot_renderer=plot_renderer,
        ),
        alert_rules=alert_rules or _alert_rules(),
    )
    return ServiceHarness(
        service=service,
        db=db,
        trajectory_repo=trajectory_repo,
        decision_repo=decision_repo,
        config_repo=config_repo,
        artifacts=artifacts,
        detector=detector,
        navigation=navigation,
    )


def test_start_auto_mission_creates_automatic_mission_and_persists_config() -> None:
    harness = _build_service()
    mission = harness.service.start_auto_mission(
        source_name="auto-source-1",
        total_frames=10,
        fps=2.0,
        nav_mode=NavMode.MARKER,
        detector_name="nanodet",
        config_json={"aruco": {"dict": "4x4_50"}},
    )
    assert mission.mode is MissionMode.AUTOMATIC
    assert mission.status == "running"
    assert harness.navigation.reset_calls == 1
    saved = harness.config_repo.get(mission.mission_id)
    assert saved == {
        "nav_mode": "marker",
        "detector": "nanodet",
        "config_json": {"aruco": {"dict": "4x4_50"}},
    }


def test_start_auto_mission_is_idempotent_for_same_source() -> None:
    harness = _build_service()
    first = harness.service.start_auto_mission(
        source_name="same-source",
        total_frames=5,
        fps=1.0,
        nav_mode=NavMode.AUTO,
        detector_name="yolo",
    )
    second = harness.service.start_auto_mission(
        source_name="same-source",
        total_frames=99,
        fps=9.0,
        nav_mode=NavMode.MARKER,
        detector_name="nanodet",
    )
    assert first.mission_id == second.mission_id
    assert harness.navigation.reset_calls == 1


def test_ingest_frame_no_detection_produces_frame_event_only() -> None:
    harness = _build_service()
    mission = harness.service.start_auto_mission(
        source_name="quiet",
        total_frames=1,
        fps=1.0,
        nav_mode=NavMode.NO_MARKER,
        detector_name="yolo",
    )

    outcome = harness.service.ingest_frame(
        mission_id=mission.mission_id,
        frame_bgr=object(),
        ts_sec=0.0,
        frame_id=1,
        image_uri="file:///tmp/1.jpg",
    )

    assert outcome == AutoFrameOutcome(
        detections=[], trajectory_point=None, alerts=[], decisions=[]
    )
    assert not harness.db.alerts
    assert harness.db.mission_frames[mission.mission_id][0].image_uri == (
        "file:///tmp/1.jpg"
    )
    assert harness.decision_repo.list_by_mission(mission.mission_id) == []


def test_ingest_frame_with_alert_persists_alert_and_decision() -> None:
    detection = Detection(
        bbox=(1.0, 2.0, 3.0, 4.0),
        score=0.9,
        label="person",
        model_name="fake",
    )
    detector = FakeDetector(queue=[[detection]])
    harness = _build_service(detector=detector)
    mission = harness.service.start_auto_mission(
        source_name="noisy",
        total_frames=1,
        fps=1.0,
        nav_mode=NavMode.NO_MARKER,
        detector_name="yolo",
    )

    outcome = harness.service.ingest_frame(
        mission_id=mission.mission_id,
        frame_bgr=object(),
        ts_sec=0.25,
        frame_id=7,
        image_uri="file:///tmp/frame.jpg",
    )

    assert len(outcome.alerts) == 1
    assert len(outcome.decisions) == 1
    assert outcome.decisions[0].kind is AutoDecisionKind.ALERT_CREATED
    alert = outcome.alerts[0]
    assert alert.frame_id == 7
    assert alert.primary_detection.score == pytest.approx(0.9)
    assert alert.image_uri.startswith("memory://missions/")
    assert harness.artifacts.stored_frames[(mission.mission_id, 7)] == alert.image_uri
    stored_event = harness.db.mission_frames[mission.mission_id][0]
    assert stored_event.image_uri == alert.image_uri


def test_ingest_frame_persists_frame_before_alert() -> None:
    detection = Detection(
        bbox=(1.0, 2.0, 3.0, 4.0),
        score=0.9,
        label="person",
        model_name="fake",
    )
    db = InMemoryDatabase()
    calls: list[str] = []
    service = AutoMissionService(
        dependencies=AutoMissionService.Dependencies(
            mission_repository=InMemoryMissionRepository(db),
            alert_repository=ForeignKeyCheckingAlertRepository(db, calls),
            frame_event_repository=RecordingFrameEventRepository(db, calls),
            trajectory_repository=InMemoryTrajectoryRepository(),
            auto_decision_repository=InMemoryAutoDecisionRepository(),
            auto_mission_config_repository=InMemoryAutoMissionConfigRepository(),
            artifact_storage=InMemoryArtifactStorage(),
            detector=FakeDetector(queue=[[detection]]),
            navigation_engine=FakeNavigationEngine(),
        ),
        alert_rules=_alert_rules(),
    )
    mission = service.start_auto_mission(
        source_name="fk-order",
        total_frames=1,
        fps=1.0,
        nav_mode=NavMode.NO_MARKER,
        detector_name="yolo",
    )

    service.ingest_frame(
        mission_id=mission.mission_id,
        frame_bgr=object(),
        ts_sec=0.0,
        frame_id=7,
        image_uri="file:///tmp/frame.jpg",
    )

    assert calls == ["frame:7", "alert:7"]


def test_ingest_frame_records_suppressed_decision_when_cooldown_blocks() -> None:
    detection = Detection(
        bbox=(1.0, 2.0, 3.0, 4.0),
        score=0.9,
        label="person",
        model_name="fake",
    )
    detector = FakeDetector(queue=[[detection], [detection]])
    harness = _build_service(
        alert_rules=_alert_rules(cooldown_sec=100.0),
        detector=detector,
    )
    mission = harness.service.start_auto_mission(
        source_name="cooldown",
        total_frames=2,
        fps=1.0,
        nav_mode=NavMode.NO_MARKER,
        detector_name="yolo",
    )

    first = harness.service.ingest_frame(
        mission_id=mission.mission_id,
        frame_bgr=object(),
        ts_sec=0.0,
        frame_id=1,
        image_uri="file:///tmp/1.jpg",
    )
    second = harness.service.ingest_frame(
        mission_id=mission.mission_id,
        frame_bgr=object(),
        ts_sec=0.5,
        frame_id=2,
        image_uri="file:///tmp/2.jpg",
    )

    assert first.decisions[0].kind is AutoDecisionKind.ALERT_CREATED
    assert not second.alerts
    assert len(second.decisions) == 1
    assert second.decisions[0].kind is AutoDecisionKind.ALERT_SUPPRESSED
    assert len(harness.db.alerts) == 1


def test_ingest_frame_writes_trajectory_point_with_sequential_seq() -> None:
    navigation = FakeNavigationEngine(
        queue=[
            TrajectoryPoint(
                mission_id="",
                seq=0,
                ts_sec=0.0,
                x=1.0,
                y=2.0,
                z=0.5,
                source=TrajectorySource.MARKER,
            ),
            None,
            TrajectoryPoint(
                mission_id="",
                seq=0,
                ts_sec=0.0,
                x=1.1,
                y=2.1,
                z=0.6,
                source=TrajectorySource.OPTICAL_FLOW,
            ),
        ],
    )
    harness = _build_service(navigation=navigation)
    mission = harness.service.start_auto_mission(
        source_name="traj",
        total_frames=3,
        fps=1.0,
        nav_mode=NavMode.MARKER,
        detector_name="nanodet",
    )

    outcomes = [
        harness.service.ingest_frame(
            mission_id=mission.mission_id,
            frame_bgr=object(),
            ts_sec=float(idx),
            frame_id=idx,
            image_uri=f"file:///tmp/{idx}.jpg",
        )
        for idx in range(1, 4)
    ]

    assert outcomes[0].trajectory_point is not None
    assert outcomes[0].trajectory_point.seq == 1
    assert outcomes[1].trajectory_point is None
    assert outcomes[2].trajectory_point is not None
    assert outcomes[2].trajectory_point.seq == 2
    stored = harness.trajectory_repo.list_by_mission(mission.mission_id)
    assert [point.seq for point in stored] == [1, 2]
    assert stored[0].source is TrajectorySource.MARKER
    assert stored[1].source is TrajectorySource.OPTICAL_FLOW


def test_ingest_frame_rejects_operator_mission() -> None:
    harness = _build_service()
    mission = harness.service.start_auto_mission(
        source_name="flip",
        total_frames=1,
        fps=1.0,
        nav_mode=NavMode.AUTO,
        detector_name="yolo",
    )
    harness.db.missions[mission.mission_id].mode = MissionMode.OPERATOR

    with pytest.raises(ValueError, match="automatic mission"):
        harness.service.ingest_frame(
            mission_id=mission.mission_id,
            frame_bgr=object(),
            ts_sec=0.0,
            frame_id=1,
            image_uri="file:///tmp/1.jpg",
        )


def test_ingest_frame_short_circuits_when_mission_already_completed() -> None:
    harness = _build_service()
    mission = harness.service.start_auto_mission(
        source_name="done",
        total_frames=1,
        fps=1.0,
        nav_mode=NavMode.AUTO,
        detector_name="yolo",
    )
    harness.db.missions[mission.mission_id].status = "completed"

    outcome = harness.service.ingest_frame(
        mission_id=mission.mission_id,
        frame_bgr=object(),
        ts_sec=0.0,
        frame_id=1,
        image_uri="file:///tmp/1.jpg",
    )
    assert outcome == AutoFrameOutcome(
        detections=[], trajectory_point=None, alerts=[], decisions=[]
    )
    assert harness.db.mission_frames[mission.mission_id] == []


def test_ingest_frame_raises_for_unknown_mission() -> None:
    harness = _build_service()
    with pytest.raises(ValueError, match="Mission not found"):
        harness.service.ingest_frame(
            mission_id="missing",
            frame_bgr=object(),
            ts_sec=0.0,
            frame_id=1,
            image_uri="file:///tmp/1.jpg",
        )


def test_complete_auto_mission_transitions_and_writes_trajectory_csv() -> None:
    navigation = FakeNavigationEngine(
        queue=[
            TrajectoryPoint(
                mission_id="",
                seq=0,
                ts_sec=0.0,
                x=0.1,
                y=0.2,
                z=0.3,
                source=TrajectorySource.MARKER,
            ),
        ],
    )
    harness = _build_service(navigation=navigation)
    mission = harness.service.start_auto_mission(
        source_name="finish",
        total_frames=1,
        fps=1.0,
        nav_mode=NavMode.MARKER,
        detector_name="nanodet",
    )
    harness.service.ingest_frame(
        mission_id=mission.mission_id,
        frame_bgr=object(),
        ts_sec=0.0,
        frame_id=1,
        image_uri="file:///tmp/1.jpg",
    )

    completed = harness.service.complete_auto_mission(
        mission_id=mission.mission_id, completed_frame_id=1
    )

    assert completed is not None
    assert completed.status == "completed"
    assert completed.completed_frame_id == 1
    key = f"{mission.created_at[:10]}:{mission.mission_id}:trajectory"
    saved = harness.artifacts._reports[key]
    assert saved["points"][0]["seq"] == 1
    assert saved["points"][0]["source"] == "marker"


def test_complete_auto_mission_is_idempotent() -> None:
    harness = _build_service()
    mission = harness.service.start_auto_mission(
        source_name="twice",
        total_frames=1,
        fps=1.0,
        nav_mode=NavMode.AUTO,
        detector_name="yolo",
    )
    first = harness.service.complete_auto_mission(mission.mission_id)
    second = harness.service.complete_auto_mission(mission.mission_id)
    assert first is not None
    assert second is not None
    assert second.status == "completed"


def test_complete_auto_mission_returns_none_for_unknown_mission() -> None:
    harness = _build_service()
    assert harness.service.complete_auto_mission("does-not-exist") is None


def test_complete_auto_mission_saves_report_and_plot() -> None:
    navigation = FakeNavigationEngine(
        queue=[
            TrajectoryPoint(
                mission_id="",
                seq=0,
                ts_sec=0.0,
                x=0.0,
                y=0.0,
                z=0.0,
                source=TrajectorySource.MARKER,
            ),
            TrajectoryPoint(
                mission_id="",
                seq=0,
                ts_sec=1.0,
                x=1.0,
                y=0.5,
                z=0.0,
                source=TrajectorySource.OPTICAL_FLOW,
            ),
        ],
    )
    plot_renderer = FakeTrajectoryPlotRenderer()
    harness = _build_service(navigation=navigation, plot_renderer=plot_renderer)
    mission = harness.service.start_auto_mission(
        source_name="report-mission",
        total_frames=2,
        fps=1.0,
        nav_mode=NavMode.MARKER,
        detector_name="nanodet",
        config_json={"foo": "bar"},
    )
    for idx in (1, 2):
        harness.service.ingest_frame(
            mission_id=mission.mission_id,
            frame_bgr=object(),
            ts_sec=float(idx - 1),
            frame_id=idx,
            image_uri=f"file:///tmp/{idx}.jpg",
        )

    completed = harness.service.complete_auto_mission(
        mission.mission_id, completed_frame_id=2
    )
    assert completed is not None

    ds = mission.created_at[:10]
    plot_key = f"{ds}:{mission.mission_id}:trajectory_plot"
    assert plot_key in harness.artifacts._reports
    assert plot_renderer.calls == [(mission.mission_id, 2)]

    report = harness.artifacts._reports[f"{ds}:{mission.mission_id}"]
    assert report["mission_id"] == mission.mission_id
    assert report["mode"] == "automatic"
    assert report["status"] == "completed"
    assert report["frames_processed"] == 2
    trajectory_block = report["trajectory"]
    assert trajectory_block["points_total"] == 2
    assert trajectory_block["by_source"]["marker"] == 1
    assert trajectory_block["by_source"]["optical_flow"] == 1
    assert trajectory_block["duration_sec"] == pytest.approx(1.0)
    assert report["decisions"] == {"alert_created": 0, "alert_suppressed": 0}
    assert report["config_snapshot"]["nav_mode"] == "marker"
    assert report["config_snapshot"]["detector"] == "nanodet"
    assert report["artifacts"]["trajectory_csv"].endswith("trajectory.csv")
    assert report["artifacts"]["trajectory_plot"].endswith("plots/trajectory.png")
    assert report["artifacts"]["report_json"].endswith("report.json")


def test_complete_auto_mission_without_renderer_leaves_plot_null() -> None:
    harness = _build_service()  # no plot_renderer
    mission = harness.service.start_auto_mission(
        source_name="no-plot",
        total_frames=0,
        fps=1.0,
        nav_mode=NavMode.NO_MARKER,
        detector_name="yolo",
    )
    completed = harness.service.complete_auto_mission(mission.mission_id)
    assert completed is not None

    ds = mission.created_at[:10]
    report = harness.artifacts._reports[f"{ds}:{mission.mission_id}"]
    assert report["artifacts"]["trajectory_plot"] is None
    assert (
        f"{ds}:{mission.mission_id}:trajectory_plot" not in harness.artifacts._reports
    )


def test_get_auto_mission_report_returns_cached_on_second_call() -> None:
    harness = _build_service()
    mission = harness.service.start_auto_mission(
        source_name="cached",
        total_frames=0,
        fps=1.0,
        nav_mode=NavMode.AUTO,
        detector_name="yolo",
    )
    harness.service.complete_auto_mission(mission.mission_id)
    ds = mission.created_at[:10]
    report_key = f"{ds}:{mission.mission_id}"

    first = harness.service.get_auto_mission_report(mission.mission_id)
    # Tamper the cached report to confirm second call reads it rather than rebuilding.
    harness.artifacts._reports[report_key] = {"sentinel": True}
    second = harness.service.get_auto_mission_report(mission.mission_id)
    assert first["mission_id"] == mission.mission_id
    assert second == {"sentinel": True}


def test_get_auto_mission_report_rejects_running_mission() -> None:
    harness = _build_service()
    mission = harness.service.start_auto_mission(
        source_name="still-running",
        total_frames=0,
        fps=1.0,
        nav_mode=NavMode.AUTO,
        detector_name="yolo",
    )
    with pytest.raises(ValueError, match="not completed"):
        harness.service.get_auto_mission_report(mission.mission_id)


def test_get_auto_mission_report_rejects_operator_mission() -> None:
    harness = _build_service()
    mission = harness.service.start_auto_mission(
        source_name="flipmode",
        total_frames=0,
        fps=1.0,
        nav_mode=NavMode.AUTO,
        detector_name="yolo",
    )
    harness.db.missions[mission.mission_id].mode = MissionMode.OPERATOR
    harness.db.missions[mission.mission_id].status = "completed"
    with pytest.raises(ValueError, match="automatic mission"):
        harness.service.get_auto_mission_report(mission.mission_id)
