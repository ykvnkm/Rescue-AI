"""Tests for :mod:`rescue_ai.infrastructure.trajectory_plot`."""

from __future__ import annotations

from rescue_ai.domain.entities import TrajectoryPoint
from rescue_ai.domain.value_objects import TrajectorySource
from rescue_ai.infrastructure.trajectory_plot import (
    MatplotlibTrajectoryPlotRenderer,
    build_trajectory_plot_renderer,
)


def _point(seq: int, x: float, y: float, source: TrajectorySource) -> TrajectoryPoint:
    return TrajectoryPoint(
        mission_id="m",
        seq=seq,
        ts_sec=float(seq),
        frame_id=seq,
        x=x,
        y=y,
        z=0.0,
        source=source,
    )


def test_render_returns_png_bytes_for_non_empty_track() -> None:
    renderer = MatplotlibTrajectoryPlotRenderer()
    points = [
        _point(1, 0.0, 0.0, TrajectorySource.MARKER),
        _point(2, 1.0, 0.5, TrajectorySource.MARKER),
        _point(3, 1.5, 1.0, TrajectorySource.OPTICAL_FLOW),
    ]
    png = renderer.render(mission_id="abcdef123456", points=points)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 512


def test_render_handles_empty_points_list() -> None:
    renderer = MatplotlibTrajectoryPlotRenderer()
    png = renderer.render(mission_id="empty-mission", points=[])
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_factory_returns_matplotlib_renderer() -> None:
    renderer = build_trajectory_plot_renderer()
    assert isinstance(renderer, MatplotlibTrajectoryPlotRenderer)
