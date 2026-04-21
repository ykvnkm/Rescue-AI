"""Altitude estimation — PnP-above-marker and scale-from-optical-flow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from rescue_ai.navigation.tuning import NavigationTuning


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


def ema_altitude_from_pnp(
    current_alt: float, cam_pos_z: float, config: NavigationTuning
) -> float:
    """EMA update of altitude state from a PnP camera-centre z-estimate."""
    return (1.0 - config.marker_alpha_alt) * current_alt + (
        config.marker_alpha_alt * float(cam_pos_z)
    )


def estimate_altitude_from_scale(
    *,
    prev_gray: np.ndarray,
    cur_gray: np.ndarray,
    prev_pts: np.ndarray | None,
    prev_scale: float | None,
    current_alt: float,
    lk_params: dict,
    config: NavigationTuning,
) -> AltitudeUpdate:
    """Altitude update from inter-frame optical-flow scale ratio.

    LK-tracks ``prev_pts`` to the current frame, computes the point-cloud
    scale change, converts it to an altitude delta, and blends via EMA.
    Re-samples features on the current frame for the next call.
    """
    new_alt = current_alt
    next_pts = prev_pts
    next_scale = prev_scale

    if prev_pts is not None:
        cv2_any: Any = cv2
        nxt, st, _ = cv2_any.calcOpticalFlowPyrLK(
            prev_gray, cur_gray, prev_pts, None, **lk_params
        )
        if nxt is not None and st is not None:
            mask = st.flatten() == 1
            p1 = nxt[mask].reshape(-1, 2)
            if len(p1) > config.alt_flow_min_tracked:
                cs = compute_scale_from_samples(p1)
                if prev_scale is not None and cs is not None and abs(prev_scale) > 1e-9:
                    ratio = cs / prev_scale
                    if config.alt_ratio_lo < ratio < config.alt_ratio_hi:
                        dh = float(
                            np.clip(
                                current_alt / ratio - current_alt,
                                -config.max_dh_alt,
                                config.max_dh_alt,
                            )
                        )
                        new_alt = (
                            1.0 - config.z_alpha_alt
                        ) * current_alt + config.z_alpha_alt * (
                            current_alt + dh * config.px_to_m_z_alt
                        )
                if cs is not None:
                    next_scale = cs

    next_pts = cv2.goodFeaturesToTrack(
        cur_gray,
        config.alt_gftt_max_corners,
        config.alt_gftt_quality,
        config.alt_gftt_min_dist,
    )
    if next_pts is not None:
        sc = compute_scale_from_samples(next_pts.reshape(-1, 2))
        if sc is not None:
            next_scale = sc
    return AltitudeUpdate(altitude=new_alt, next_pts=next_pts, next_scale=next_scale)
