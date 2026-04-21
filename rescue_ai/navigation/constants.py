"""Navigation tuning constants ported verbatim from diplom-prod.

Values must stay byte-identical to the upstream pipeline so the golden
trajectory regression (env-gated) keeps passing.
"""

from __future__ import annotations

import numpy as np

# Marker geometry: unit square centred at origin, scaled by marker_size_m at use site.
MARKER_3D = np.array(
    [[-0.5, -0.5, 0], [0.5, -0.5, 0], [0.5, 0.5, 0], [-0.5, 0.5, 0]],
    dtype=np.float32,
)

# Default intrinsics for the marker pipeline (rescaled internally to MARKER_RESIZE_*).
NAV_W = 853
NAV_H = 480
FX_NAV = 1100.0
FY_NAV = 1100.0

# Marker pipeline working resolution.
MARKER_RESIZE_W = 960
MARKER_RESIZE_H = 540
MARKER_SIZE_XY_M = 2.0

# Init/probe window.
AUTO_MARKER_SECONDS = 3.0

# Altitude (scale-from-optical-flow) tuning.
PX_TO_M_Z_ALT = 0.02
MAX_DH_ALT = 0.4
Z_ALPHA_ALT = 0.25
MARKER_ALPHA_ALT = 0.3

# Laplacian smoothing for jumps.
SMOOTH_WINDOW = 5
SMOOTH_LR_XY = 0.15
SMOOTH_LR_Z = 0.25
SMOOTH_JUMP_XY = 0.7
SMOOTH_JUMP_Z = 0.3

# Marker-region-of-interest mask for feature detection.
ROI_TOP_RATIO = 0.45

# Good-features-to-track parameters for marker pipeline.
MAX_CORNERS_MARKER = 800
MIN_TRACK_PTS_MARKER = 120
GFTT_QUALITY = 0.01
GFTT_MIN_DIST = 12
GFTT_BLOCK = 7

# LK + RANSAC tuning.
LK_WIN_MARKER = 31
LK_LEVELS_MARKER = 4
REDETECT_MIN_PTS_MARKER = 0
MAX_STEP_SCALE_MARKER = 1.3
RANSAC_THR_MARKER = 3.0
MIN_INLIERS_MARKER = 140
MIN_INLIER_RATIO_MARKER = 0.35
HARD_RESET_RATIO_MARKER = 0.25
FB_THR_MARKER = 1.5
LK_ERR_THR_MARKER = 25.0
MAX_SPEED_MARKER = 9.0
MARKER_CHECK_EVERY = 45
MARKER_AREA_MIN = 3500.0

# Coordinate flips (drone-frame convention).
FLIP_X_MARKER = False
FLIP_Y_MARKER = True
