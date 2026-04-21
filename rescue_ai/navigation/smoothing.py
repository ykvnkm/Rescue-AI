"""Laplacian-graph smoothing for trajectory jump correction."""

from __future__ import annotations

import numpy as np

from rescue_ai.navigation.tuning import NavigationTuning


def laplacian_smooth_window(
    traj_points: list[np.ndarray],
    pos: np.ndarray,
    config: NavigationTuning,
) -> np.ndarray:
    """Apply one Laplacian-smoothing gradient step to ``pos``.

    Builds a path-graph Laplacian over the last ``config.smooth_window``
    points plus ``pos``, then nudges ``pos`` along the negative gradient
    with separate learning rates for xy and z. No-op when fewer than 3
    effective points are available.
    """
    if len(traj_points) < 2:
        return pos
    window_start = -config.smooth_window
    win = traj_points[window_start:] + [pos]
    n = len(win)
    if n < 3:
        return pos
    A = np.zeros((n, n), dtype=float)
    for i in range(n - 1):
        A[i, i + 1] = 1.0
        A[i + 1, i] = 1.0
    D = np.diag(A.sum(axis=1))
    L = D - A
    X = np.vstack(win)
    grad = 2.0 * (L @ X)[-1]
    pos_smoothed = pos.copy()
    pos_smoothed[:2] -= config.smooth_lr_xy * grad[:2]
    pos_smoothed[2] -= config.smooth_lr_z * grad[2]
    return pos_smoothed
