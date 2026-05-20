"""Navigation tuning — camera-/scene-dependent algorithmic knobs.

This is *not* an environment config (see ``rescue_ai.config`` for env
settings). ``NavigationTuning`` bundles algorithm parameters that the
caller can override per-mission (different camera, different altitude
prior, different marker size). Defaults match the diplom-prod pipeline
so the golden trajectory regression stays byte-identical.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NavigationTuning:
    """Immutable set of navigation knobs.

    Grouped by sub-system. Frozen so engines can cache derived values
    (intrinsics, LK params) without worrying about mutation.
    """

    # ── Camera intrinsics (reference resolution) ────────────────────
    nav_width: int = 853
    nav_height: int = 480
    fx_nav: float = 1100.0
    fy_nav: float = 1100.0

    # ── Marker-pipeline working resolution ──────────────────────────
    marker_resize_w: int = 960
    marker_resize_h: int = 540
    marker_size_xy_m: float = 2.0

    # ── Runtime / init buffer ───────────────────────────────────────
    fps: float = 30.0
    auto_marker_seconds: float = 3.0
    initial_altitude_m: float = 1.5

    # ── Marker detection / init candidate filter ────────────────────
    marker_area_min: float = 3500.0
    marker_area_min_init: float = 3000.0
    marker_area_max_ratio_init: float = 0.5
    marker_check_every: int = 45
    marker_check_dist_px: float = 30.0
    marker_init_topk: int = 40

    # ── Altitude (PnP + flow-scale) ─────────────────────────────────
    px_to_m_z_alt: float = 0.02
    max_dh_alt: float = 0.4
    z_alpha_alt: float = 0.25
    marker_alpha_alt: float = 0.3
    alt_gftt_max_corners: int = 500
    alt_gftt_quality: float = 0.01
    alt_gftt_min_dist: int = 7
    alt_flow_min_tracked: int = 40
    alt_ratio_lo: float = 0.7
    alt_ratio_hi: float = 1.3
    alt_marker_lost_cnt: int = 10

    # ── Trajectory smoothing (no-marker branch only, per upstream) ──
    smooth_window: int = 5
    smooth_lr_xy: float = 0.15
    smooth_lr_z: float = 0.25
    smooth_jump_xy: float = 0.7
    smooth_jump_z: float = 0.3

    # ── ROI for marker tracking ─────────────────────────────────────
    roi_top_ratio: float = 0.45

    # ── GFTT for marker tracking ────────────────────────────────────
    max_corners_marker: int = 800
    min_track_pts_marker: int = 120
    gftt_quality: float = 0.01
    gftt_min_dist: int = 12
    gftt_block: int = 7

    # ── LK + RANSAC for marker tracking ─────────────────────────────
    lk_win_marker: int = 31
    lk_levels_marker: int = 4
    redetect_min_pts_marker: int = 0
    max_step_scale_marker: float = 1.3
    ransac_thr_marker: float = 3.0
    min_inliers_marker: int = 140
    min_inlier_ratio_marker: float = 0.35
    hard_reset_ratio_marker: float = 0.25
    fb_thr_marker: float = 1.5
    lk_err_thr_marker: float = 25.0
    max_speed_marker: float = 9.0

    # ── No-marker pipeline ──────────────────────────────────────────
    no_marker_max_corners: int = 1200
    no_marker_quality: float = 0.01
    no_marker_min_dist: int = 7
    no_marker_min_pts: int = 50
    no_marker_min_matched: int = 20
    no_marker_flow_alpha: float = 0.2
    no_marker_max_step_px: float = 80.0

    # ── Coordinate flips (drone-frame convention) ───────────────────
    flip_x_marker: bool = False
    flip_y_marker: bool = True
