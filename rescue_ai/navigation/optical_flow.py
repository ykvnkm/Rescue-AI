"""Optical-flow utilities and scale-based altitude estimation."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from rescue_ai.navigation.constants import MAX_DH_ALT, PX_TO_M_Z_ALT, Z_ALPHA_ALT


def compute_scale_from_samples(samples: np.ndarray | None) -> float | None:
    """Median radial spread of a point cloud — proxy for camera-to-scene scale."""
    if samples is None or len(samples) < 2:
        return None
    c = np.median(samples, axis=0)
    d = np.linalg.norm(samples - c, axis=1)
    return float(np.median(d)) if len(d) else None


@dataclass
class AltitudeUpdate:
    """Result of one altitude update step from optical flow."""

    altitude: float
    next_pts: np.ndarray | None
    next_scale: float | None


def update_altitude_from_flow(  # pylint: disable=too-many-arguments,too-many-locals
    *,
    prev_gray: np.ndarray,
    cur_gray: np.ndarray,
    prev_pts: np.ndarray | None,
    prev_scale: float | None,
    current_alt: float,
    lk_params: dict,
    min_tracked: int = 40,
    ratio_lo: float = 0.7,
    ratio_hi: float = 1.3,
) -> AltitudeUpdate:
    """Update altitude state from inter-frame point-cloud scale change.

    Pure function: takes previous gray + tracked points + scale, computes
    LK forward, derives a new scale and an EMA-blended altitude. Returns
    the next state to feed back next frame.
    """
    new_alt = current_alt
    next_pts = prev_pts
    next_scale = prev_scale

    if prev_pts is not None:
        nxt, st, _ = cv2.calcOpticalFlowPyrLK(  # type: ignore[call-overload]
            prev_gray, cur_gray, prev_pts, None, **lk_params
        )
        if nxt is not None and st is not None:
            mask = st.flatten() == 1
            p1 = nxt[mask].reshape(-1, 2)
            if len(p1) > min_tracked:
                cs = compute_scale_from_samples(p1)
                if prev_scale is not None and cs is not None and abs(prev_scale) > 1e-9:
                    ratio = cs / prev_scale
                    if ratio_lo < ratio < ratio_hi:
                        dh = float(
                            np.clip(
                                current_alt / ratio - current_alt,
                                -MAX_DH_ALT,
                                MAX_DH_ALT,
                            )
                        )
                        new_alt = (1.0 - Z_ALPHA_ALT) * current_alt + Z_ALPHA_ALT * (
                            current_alt + dh * PX_TO_M_Z_ALT
                        )
                if cs is not None:
                    next_scale = cs

    next_pts = cv2.goodFeaturesToTrack(cur_gray, 500, 0.01, 7)
    if next_pts is not None:
        sc = compute_scale_from_samples(next_pts.reshape(-1, 2))
        if sc is not None:
            next_scale = sc
    return AltitudeUpdate(altitude=new_alt, next_pts=next_pts, next_scale=next_scale)
