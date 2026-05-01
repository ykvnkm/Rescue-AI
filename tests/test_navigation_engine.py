"""Behavioural tests for NavigationEngine using synthetic frames."""

from __future__ import annotations

import numpy as np
import pytest

from rescue_ai.domain.entities import TrajectoryPoint
from rescue_ai.domain.value_objects import NavMode, TrajectorySource
from rescue_ai.navigation import engine as engine_module
from rescue_ai.navigation.engine import NavigationEngine
from rescue_ai.navigation.tuning import NavigationTuning


def _blank_frame(config: NavigationTuning) -> np.ndarray:
    return np.zeros((config.marker_resize_h, config.marker_resize_w, 3), dtype=np.uint8)


def _red_marker_frame(config: NavigationTuning, half: int = 80) -> np.ndarray:
    """Pure-red square on a black background — triggers the marker detector."""
    frame = _blank_frame(config)
    cx = config.marker_resize_w // 2
    cy = config.marker_resize_h // 2
    y0, y1 = cy - half, cy + half
    x0, x1 = cx - half, cx + half
    frame[y0:y1, x0:x1] = (0, 0, 255)
    return frame


def _textured_frame(config: NavigationTuning, seed: int = 0) -> np.ndarray:
    """Random gray-ish frame — gives optical-flow features to track."""
    rng = np.random.default_rng(seed)
    return rng.integers(
        0, 255, size=(config.nav_height, config.nav_width, 3), dtype=np.uint8
    )


def test_engine_buffers_during_init_returns_none() -> None:
    config = NavigationTuning(fps=30.0, auto_marker_seconds=0.1)
    engine = NavigationEngine(mission_id="m-test", config=config)
    engine.reset()
    out = engine.step(_red_marker_frame(config), ts_sec=0.0, frame_id=0)
    assert out is None  # need 3 frames for fps=30 * 0.1


def test_engine_emits_marker_init_point_when_marker_found() -> None:
    config = NavigationTuning(fps=10.0, auto_marker_seconds=0.1)
    engine = NavigationEngine(mission_id="m-test", config=config)
    engine.reset()
    points = []
    for i in range(2):
        out = engine.step(_red_marker_frame(config), ts_sec=float(i) * 0.1, frame_id=i)
        if out is not None:
            points.append(out)
    assert points, "engine should emit at least the init point"
    p = points[0]
    assert p.seq == 0
    assert p.source is TrajectorySource.MARKER
    assert p.mission_id == "m-test"


def test_engine_falls_back_to_no_marker_when_no_marker_in_buffer() -> None:
    config = NavigationTuning(fps=10.0, auto_marker_seconds=0.2)
    engine = NavigationEngine(mission_id="m-test", config=config)
    engine.reset()
    # Blank frames → no marker detected → fallback to OPTICAL_FLOW.
    points = []
    for i in range(3):
        out = engine.step(
            _textured_frame(config, seed=i), ts_sec=float(i) * 0.1, frame_id=i
        )
        if out is not None:
            points.append(out)
    assert points, "engine should emit at least the no-marker init point"
    p0 = points[0]
    assert p0.seq == 0
    assert p0.source is TrajectorySource.OPTICAL_FLOW
    assert p0.x == 0.0 and p0.y == 0.0


def test_engine_reset_clears_state() -> None:
    config = NavigationTuning(fps=10.0, auto_marker_seconds=0.3)
    engine = NavigationEngine(mission_id="m-test", config=config)
    engine.reset()
    for i in range(3):
        engine.step(_red_marker_frame(config), ts_sec=float(i) * 0.1, frame_id=i)
    engine.reset()
    # After reset, init buffer is empty again — first frame must return None.
    out = engine.step(_red_marker_frame(config), ts_sec=0.0, frame_id=0)
    assert out is None  # back to init phase, needs more frames


def test_engine_step_rejects_non_image_input() -> None:
    engine = NavigationEngine(mission_id="m-test")
    engine.reset()
    assert engine.step(np.zeros((10, 10), dtype=np.uint8), ts_sec=0.0) is None
    assert engine.step(np.zeros((10, 10, 4), dtype=np.uint8), ts_sec=0.0) is None


def test_engine_forced_no_marker_skips_init_buffer() -> None:
    """ADR-0007 / diplom-prod parity: when nav_mode=NO_MARKER is forced
    (e.g. detection enabled), the engine emits a point on the very first
    frame instead of buffering ``auto_marker_seconds * fps`` frames."""
    config = NavigationTuning(fps=10.0, auto_marker_seconds=3.0)
    engine = NavigationEngine(mission_id="m-test", config=config)
    engine.reset(nav_mode=NavMode.NO_MARKER, fps=6.0)

    out = engine.step(_textured_frame(config, seed=1), ts_sec=0.0, frame_id=0)

    assert out is not None
    assert out.seq == 0
    assert out.source is TrajectorySource.OPTICAL_FLOW
    assert out.x == 0.0 and out.y == 0.0


def test_engine_forced_marker_does_auto_probe() -> None:
    """nav_mode=MARKER still buffers init frames and probes for marker."""
    config = NavigationTuning(fps=10.0, auto_marker_seconds=0.1)
    engine = NavigationEngine(mission_id="m-test", config=config)
    engine.reset(nav_mode=NavMode.MARKER)

    out0 = engine.step(_red_marker_frame(config), ts_sec=0.0, frame_id=0)
    # First frame with auto_marker_seconds=0.1 * fps=10 = 1 frame init,
    # so the engine commits immediately.
    assert out0 is not None
    assert out0.source is TrajectorySource.MARKER


def test_engine_reset_updates_fps_into_tuning() -> None:
    config = NavigationTuning(fps=30.0, auto_marker_seconds=0.1)
    engine = NavigationEngine(mission_id="m-test", config=config)
    engine.reset(fps=6.0)
    # Internal config now reflects the per-mission fps.
    assert abs(engine._config.fps - 6.0) < 1e-9


def test_engine_emits_monotonic_seq_after_init() -> None:
    config = NavigationTuning(fps=10.0, auto_marker_seconds=0.1)
    engine = NavigationEngine(mission_id="m-test", config=config)
    engine.reset()
    seqs = []
    for i in range(5):
        out = engine.step(_red_marker_frame(config), ts_sec=float(i) * 0.1, frame_id=i)
        if out is not None:
            seqs.append(out.seq)
    # First is 0, then strictly increasing.
    assert seqs[0] == 0
    assert all(seqs[i] < seqs[i + 1] for i in range(len(seqs) - 1))


def test_marker_init_replays_buffer_tail_before_next_external_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marker mode must not drop init-window frames after the chosen seed.

    diplom-prod seeds from the best init marker frame and then advances
    the marker tracker through the rest of the buffered frames before it
    reads new frames. This keeps LK/RANSAC state aligned with the actual
    source frame stream.
    """

    steps: list[int | None] = []

    class FakeMarkerEngine:
        """Minimal marker-engine stub used to track replayed frame ids."""

        def __init__(self, mission_id: str, config: NavigationTuning) -> None:
            _ = config
            self._mission_id = mission_id
            self._seq = 0

        def seed_from_init(
            self, best_pts4: np.ndarray, best_frame_bgr: np.ndarray
        ) -> tuple[float, float, float]:
            _ = (best_pts4, best_frame_bgr)
            return (0.0, 0.0, 1.5)

        def set_last_accept_ts(self, ts_sec: float) -> None:
            _ = ts_sec

        def reset_sequence(self) -> None:
            self._seq = 0

        def step(
            self,
            frame_marker: np.ndarray,
            ts_sec: float,
            frame_id: int | None,
        ) -> TrajectoryPoint:
            _ = frame_marker
            steps.append(frame_id)
            self._seq += 1
            return TrajectoryPoint(
                mission_id=self._mission_id,
                seq=self._seq,
                ts_sec=ts_sec,
                frame_id=frame_id,
                x=float(frame_id or 0),
                y=0.0,
                z=1.5,
                source=TrajectorySource.MARKER,
            )

    pts = np.array(
        [[10.0, 10.0], [50.0, 10.0], [50.0, 50.0], [10.0, 50.0]],
        dtype=np.float32,
    )
    monkeypatch.setattr(engine_module, "MarkerEngine", FakeMarkerEngine)
    monkeypatch.setattr(
        engine_module, "detect_red_marker_corners", lambda _frame: (pts, None)
    )
    monkeypatch.setattr(engine_module, "polygon_area", lambda _pts: 4000.0)

    config = NavigationTuning(fps=10.0, auto_marker_seconds=0.3)
    engine = NavigationEngine(mission_id="m-test", config=config)
    engine.reset(nav_mode=NavMode.MARKER)

    frames = [np.full((120, 200, 3), idx, dtype=np.uint8) for idx in range(4)]
    assert engine.step(frames[0], ts_sec=0.0, frame_id=0) is None
    assert engine.step(frames[1], ts_sec=0.1, frame_id=1) is None
    init_point = engine.step(frames[2], ts_sec=0.2, frame_id=2)
    next_point = engine.step(frames[3], ts_sec=0.3, frame_id=3)

    assert init_point is not None
    assert init_point.frame_id == 0
    assert next_point is not None
    assert next_point.frame_id == 3
    assert steps == [0, 1, 2, 3]
