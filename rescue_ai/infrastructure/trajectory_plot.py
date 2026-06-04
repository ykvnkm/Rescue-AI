"""Matplotlib-based :class:`TrajectoryPlotRendererPort` adapter.

Рендерит сводный PNG-файл с тремя подграфиками маршрута миссии: 3D-вид,
вид сверху на плоскости XY и зависимость высоты от времени Z(t). Состав
подграфиков и их цветовая палитра соответствуют оформлению клиентского
интерфейса автоматического режима в ``pilot_ui.html``, что обеспечивает
единое визуальное представление маршрута в реальном времени и в итоговом
файле отчёта.

Matplotlib импортируется лениво и переключается на бэкенд ``Agg``, что
позволяет выполнять отрисовку в безголовом окружении (контейнеры,
CI-конвейер). При отсутствии библиотеки фабрика
``build_trajectory_plot_renderer`` выдаёт ошибку, и точка сборки
зависимостей фиксирует отсутствие реализации программного соглашения.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from io import BytesIO

from rescue_ai.domain.entities import TrajectoryPoint

_COLOR_3D_LINE = "#f97316"
_COLOR_3D_MARKER = "#fb923c"
_COLOR_XY_LINE = "#22d3ee"
_COLOR_XY_MARKER = "#38bdf8"
_COLOR_Z_LINE = "#f97316"


class MatplotlibTrajectoryPlotRenderer:
    """Render a 3-panel trajectory figure: 3D path, XY top-down, Z(t)."""

    def __init__(
        self,
        *,
        figsize: tuple[float, float] = (10.0, 7.5),
        dpi: int = 120,
    ):
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

        fig = plt.figure(figsize=self._figsize, dpi=self._dpi)
        gs = fig.add_gridspec(
            2,
            2,
            height_ratios=[1.4, 1.0],
            hspace=0.32,
            wspace=0.28,
        )
        ax_3d = fig.add_subplot(gs[0, :], projection="3d")
        ax_xy = fig.add_subplot(gs[1, 0])
        ax_zt = fig.add_subplot(gs[1, 1])

        try:
            if not points:
                ax_3d.text2D(
                    0.5,
                    0.5,
                    "no trajectory points",
                    ha="center",
                    va="center",
                    transform=ax_3d.transAxes,
                )
                ax_xy.text(
                    0.5,
                    0.5,
                    "no trajectory points",
                    ha="center",
                    va="center",
                    transform=ax_xy.transAxes,
                )
                ax_zt.text(
                    0.5,
                    0.5,
                    "no trajectory points",
                    ha="center",
                    va="center",
                    transform=ax_zt.transAxes,
                )
            else:
                ordered = sorted(points, key=lambda item: item.seq)
                xs = [p.x for p in ordered]
                ys = [p.y for p in ordered]
                zs = [p.z for p in ordered]
                ts = [p.ts_sec for p in ordered]

                ax_3d.plot(xs, ys, zs, color=_COLOR_3D_LINE, linewidth=1.6)
                ax_3d.scatter(xs, ys, zs, s=10, color=_COLOR_3D_MARKER)
                ax_3d.set_xlabel("X, м")
                ax_3d.set_ylabel("Y, м")
                ax_3d.set_zlabel("Z, м")
                ax_3d.set_title("Траектория 3D", fontsize=11)

                ax_xy.plot(xs, ys, color=_COLOR_XY_LINE, linewidth=1.3)
                ax_xy.scatter(xs, ys, s=14, color=_COLOR_XY_MARKER)
                ax_xy.set_aspect("equal", adjustable="datalim")
                ax_xy.set_xlabel("X, м")
                ax_xy.set_ylabel("Y, м")
                ax_xy.set_title("Плоскость XY", fontsize=11)
                ax_xy.grid(True, linestyle=":", linewidth=0.5, alpha=0.7)

                ax_zt.plot(ts, zs, color=_COLOR_Z_LINE, linewidth=1.4)
                ax_zt.set_xlabel("t, сек")
                ax_zt.set_ylabel("Z, м")
                ax_zt.set_title("Высота Z(t)", fontsize=11)
                ax_zt.grid(True, linestyle=":", linewidth=0.5, alpha=0.7)

            fig.suptitle(
                f"Trajectory · mission {mission_id[:8]}",
                fontsize=12,
                y=0.995,
            )
            fig.subplots_adjust(
                left=0.08,
                right=0.96,
                top=0.94,
                bottom=0.07,
                hspace=0.32,
                wspace=0.28,
            )
            buffer = BytesIO()
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
