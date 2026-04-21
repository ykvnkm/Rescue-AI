"""No-marker odometry — LK optical flow with phase-correlation fallback.

When the marker probe fails at mission start the engine falls back to a
simpler pipeline that estimates per-frame pixel shift via LK on a sparse
feature set, EMA-smooths it, and integrates into a 2-D trajectory. Z is
held at zero (or at a fixed altitude when the caller passes one).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from rescue_ai.navigation.tuning import NavigationTuning


@dataclass
class FlowShift:
    """Result of one no-marker flow step."""

    dx: float
    dy: float
    next_pts: np.ndarray | None
    used_fallback: bool  # True if phaseCorrelate was used instead of LK


def optical_flow_lk_or_phase(
    prev_gray: np.ndarray,
    cur_gray: np.ndarray,
    prev_pts: np.ndarray | None,
    lk_params: dict,
    config: NavigationTuning,
) -> FlowShift:
    """Sparse LK flow with phaseCorrelate fallback.

    Returns ``(dx, dy)`` in pixels (median of per-point LK displacements),
    along with the refreshed feature set for the next call. When LK does
    not yield enough matches, falls back to dense phase correlation over
    the whole frame.
    """
    if prev_pts is None or len(prev_pts) < config.no_marker_min_pts:
        prev_pts = cv2.goodFeaturesToTrack(
            prev_gray,
            config.no_marker_max_corners,
            config.no_marker_quality,
            config.no_marker_min_dist,
        )

    dx = dy = 0.0
    got_shift = False
    next_pts: np.ndarray | None = prev_pts

    if prev_pts is not None:
        cv2_any: Any = cv2
        nxt, st, _ = cv2_any.calcOpticalFlowPyrLK(
            prev_gray, cur_gray, prev_pts, None, **lk_params
        )
        if nxt is not None and st is not None:
            p0 = prev_pts[st.flatten() == 1].reshape(-1, 2)
            p1 = nxt[st.flatten() == 1].reshape(-1, 2)
            if len(p0) >= config.no_marker_min_matched:
                deltas = p1 - p0
                dx = float(np.median(deltas[:, 0]))
                dy = float(np.median(deltas[:, 1]))
                got_shift = True
                next_pts = p1.reshape(-1, 1, 2)
            else:
                next_pts = cv2.goodFeaturesToTrack(
                    cur_gray,
                    config.no_marker_max_corners,
                    config.no_marker_quality,
                    config.no_marker_min_dist,
                )

    if not got_shift:
        prev32 = prev_gray.astype(np.float32)
        cur32 = cur_gray.astype(np.float32)
        shift, _ = cv2.phaseCorrelate(prev32, cur32)
        dx = float(shift[0])
        dy = float(shift[1])

    return FlowShift(dx=dx, dy=dy, next_pts=next_pts, used_fallback=not got_shift)
