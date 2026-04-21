"""Laplacian-graph smoothing for trajectory jump correction."""

from __future__ import annotations

import numpy as np

from rescue_ai.navigation.constants import SMOOTH_LR_XY, SMOOTH_LR_Z, SMOOTH_WINDOW


def laplacian_smooth_last(
    traj_points: list[np.ndarray],
    pos: np.ndarray,
    window: int = SMOOTH_WINDOW,
    lr_xy: float = SMOOTH_LR_XY,
    lr_z: float = SMOOTH_LR_Z,
) -> np.ndarray:
    """Apply one Laplacian-smoothing gradient step to ``pos`` in place of last.

    Builds a path-graph Laplacian over the last ``window`` points plus
    ``pos``, then nudges ``pos`` along the negative gradient with separate
    learning rates for xy and z. No-op when fewer than 3 effective points.
    """
    if len(traj_points) < 2:
        return pos
    win = traj_points[-window:] + [pos]
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
    pos_smoothed[:2] -= lr_xy * grad[:2]
    pos_smoothed[2] -= lr_z * grad[2]
    return pos_smoothed
