"""Behavioural tests for MarkerEngine using synthetic frames."""

from __future__ import annotations

import numpy as np

from rescue_ai.domain.value_objects import TrajectorySource
from rescue_ai.navigation.constants import MARKER_RESIZE_H, MARKER_RESIZE_W
from rescue_ai.navigation.engine import MarkerEngine, MarkerEngineConfig


def _blank_frame() -> np.ndarray:
    return np.zeros((MARKER_RESIZE_H, MARKER_RESIZE_W, 3), dtype=np.uint8)


def _red_marker_frame(
    cx: int = MARKER_RESIZE_W // 2, cy: int = MARKER_RESIZE_H // 2, half: int = 80
) -> np.ndarray:
    """Pure-red square on a black background — triggers the marker detector."""
    frame = _blank_frame()
    frame[cy - half : cy + half, cx - half : cx + half] = (0, 0, 255)  # noqa: E203
    return frame


def test_engine_buffers_during_init_returns_none() -> None:
    engine = MarkerEngine(
        mission_id="m-test",
        config=MarkerEngineConfig(fps=30.0, auto_marker_seconds=0.1),
    )
    engine.reset()
    out = engine.step(_red_marker_frame(), ts_sec=0.0, frame_id=0)
    assert out is None  # need 3 frames for fps=30 * 0.1


def test_engine_init_emits_first_point_with_seq_zero_and_marker_source() -> None:
    engine = MarkerEngine(
        mission_id="m-test",
        config=MarkerEngineConfig(fps=10.0, auto_marker_seconds=0.1),
    )
    engine.reset()
    points = []
    for i in range(2):
        out = engine.step(_red_marker_frame(), ts_sec=float(i) * 0.1, frame_id=i)
        if out is not None:
            points.append(out)
    assert points, "engine should emit at least the init point"
    p = points[0]
    assert p.seq == 0
    assert p.source is TrajectorySource.MARKER
    assert p.mission_id == "m-test"


def test_engine_init_returns_none_without_marker_in_buffer() -> None:
    engine = MarkerEngine(
        mission_id="m-test",
        config=MarkerEngineConfig(fps=10.0, auto_marker_seconds=0.1),
    )
    engine.reset()
    for i in range(2):
        out = engine.step(_blank_frame(), ts_sec=float(i) * 0.1, frame_id=i)
        assert out is None


def test_engine_reset_clears_state() -> None:
    engine = MarkerEngine(
        mission_id="m-test",
        config=MarkerEngineConfig(fps=10.0, auto_marker_seconds=0.3),
    )
    engine.reset()
    for i in range(3):
        engine.step(_red_marker_frame(), ts_sec=float(i) * 0.1, frame_id=i)
    engine.reset()
    # After reset, init buffer is empty again — first frame must return None.
    out = engine.step(_red_marker_frame(), ts_sec=0.0, frame_id=0)
    assert out is None  # back to init phase, needs more frames


def test_engine_step_rejects_non_image_input() -> None:
    engine = MarkerEngine(mission_id="m-test")
    engine.reset()
    assert engine.step(np.zeros((10, 10), dtype=np.uint8), ts_sec=0.0) is None
    assert engine.step(np.zeros((10, 10, 4), dtype=np.uint8), ts_sec=0.0) is None
