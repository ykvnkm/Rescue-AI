"""Matplotlib-based :class:`TrajectoryPlotRendererPort` adapter.

Renders the XY top-down view of a mission trajectory and returns the
PNG bytes. The renderer is colour-coded by :class:`TrajectorySource`
(marker, optical_flow, fallback) so mixed-mode tracks remain readable.

Matplotlib is imported lazily with the ``Agg`` backend so the adapter
works headless (docker / CI). If matplotlib is missing,
``build_trajectory_plot_renderer`` raises — the caller wires it in only
when plots are wanted.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from io import BytesIO

from rescue_ai.domain.entities import TrajectoryPoint
from rescue_ai.domain.value_objects import TrajectorySource

_SOURCE_COLOURS: dict[TrajectorySource, str] = {
    TrajectorySource.MARKER: "#1f77b4",
    TrajectorySource.OPTICAL_FLOW: "#2ca02c",
    TrajectorySource.FALLBACK: "#d62728",
}
_DEFAULT_COLOUR = "#7f7f7f"


class MatplotlibTrajectoryPlotRenderer:
    """Render a trajectory XY plot via matplotlib."""

    def __init__(self, *, figsize: tuple[float, float] = (6.4, 4.8), dpi: int = 120):
        self._figsize = figsize
        self._dpi = dpi

    def render(
        self,
        mission_id: str,
        points: Sequence[TrajectoryPoint],
    ) -> bytes:
        import matplotlib  # noqa: WPS433  (lazy import)

        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt  # noqa: WPS433

        fig, ax = plt.subplots(figsize=self._figsize, dpi=self._dpi)
        try:
            if not points:
                ax.text(
                    0.5,
                    0.5,
                    "no trajectory points",
                    ha="center",
                    va="center",
                    transform=ax.transAxes,
                )
            else:
                ordered = sorted(points, key=lambda item: item.seq)
                xs = [point.x for point in ordered]
                ys = [point.y for point in ordered]
                ax.plot(xs, ys, color="#cccccc", linewidth=1.0, zorder=1)
                for source in TrajectorySource:
                    subset = [point for point in ordered if point.source == source]
                    if not subset:
                        continue
                    ax.scatter(
                        [point.x for point in subset],
                        [point.y for point in subset],
                        s=14,
                        color=_SOURCE_COLOURS.get(source, _DEFAULT_COLOUR),
                        label=str(source),
                        zorder=2,
                    )
                ax.legend(loc="best", fontsize=8)

            ax.set_aspect("equal", adjustable="datalim")
            ax.grid(True, linestyle=":", linewidth=0.5, alpha=0.7)
            ax.set_xlabel("x, m")
            ax.set_ylabel("y, m")
            ax.set_title(f"Trajectory · mission {mission_id[:8]}")

            buffer = BytesIO()
            fig.tight_layout()
            fig.savefig(buffer, format="png")
            return buffer.getvalue()
        finally:
            plt.close(fig)


def build_trajectory_plot_renderer() -> MatplotlibTrajectoryPlotRenderer:
    """Build the default matplotlib renderer.

    Raises :class:`RuntimeError` if matplotlib is not importable so the
    composition root surfaces the missing dependency clearly.
    """
    if importlib.util.find_spec("matplotlib") is None:
        error = ImportError("No module named 'matplotlib'")
        raise RuntimeError(
            "matplotlib is required for trajectory plots; "
            "install the `plots` extra or add matplotlib to dependencies"
        ) from error
    return MatplotlibTrajectoryPlotRenderer()
