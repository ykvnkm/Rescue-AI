"""LK+RANSAC homography tracking and ground-plane projection.

Bundles all utilities used to propagate the marker plane from frame to
frame: image preprocessing, ROI masks, homography math, and the LK +
RANSAC step itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import cv2
import numpy as np

from rescue_ai.navigation.tuning import NavigationTuning

# ── Image preprocessing ─────────────────────────────────────────────


def preprocess_gray(
    frame_bgr: np.ndarray,
    use_clahe: bool = False,
    clahe_clip: float = 2.0,
    clahe_grid: int = 8,
) -> np.ndarray:
    """BGR → grayscale, optionally CLAHE-equalised."""
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    if use_clahe:
        g = max(2, int(clahe_grid))
        clahe = cv2.createCLAHE(clipLimit=float(clahe_clip), tileGridSize=(g, g))
        gray = clahe.apply(gray)
    return gray


def make_roi_mask(gray: np.ndarray, roi_top_ratio: float = 0.45) -> np.ndarray:
    """Return a binary mask covering only the bottom (1 − roi_top_ratio) part."""
    h, w = gray.shape
    m = np.zeros((h, w), dtype=np.uint8)
    row_start = int(h * roi_top_ratio)
    m[row_start:, :] = 255
    return m


# ── Homography primitives ───────────────────────────────────────────


def safe_inv_homography(
    H: np.ndarray | None,
) -> np.ndarray | None:
    """Invert a 3×3 homography with conditioning + finite-value guards.

    Returns None when the matrix is missing, ill-conditioned (cond > 1e7),
    contains non-finite entries, or fails to invert.
    """
    if H is None:
        return None
    Hf: np.ndarray = H.astype(np.float64)
    if not np.isfinite(Hf).all():
        return None

    if abs(Hf[2, 2]) > 1e-12:
        Hf = Hf / Hf[2, 2]

    try:
        cond = np.linalg.cond(Hf)
    except (np.linalg.LinAlgError, ValueError):
        return None
    if not np.isfinite(cond) or cond > 1e7:
        return None

    try:
        invH = np.linalg.inv(Hf)
    except np.linalg.LinAlgError:
        return None

    if not np.isfinite(invH).all():
        return None
    if abs(invH[2, 2]) > 1e-12:
        invH = invH / invH[2, 2]
    return invH


def project_point(
    H_img_to_plane: np.ndarray, x: float, y: float
) -> tuple[float, float] | None:
    """Project a single image point through a homography to the plane."""
    pt = np.array([[[float(x), float(y)]]], dtype=np.float32)
    out = cv2.perspectiveTransform(pt, H_img_to_plane.astype(np.float32))[0, 0]
    if not np.isfinite(out).all():
        return None
    return float(out[0]), float(out[1])


def project_points_median(
    H_img_to_plane: np.ndarray, pts_xy: np.ndarray
) -> tuple[float, float] | None:
    """Project N image points through H and return the per-axis median.

    Returns None if fewer than 10 finite-projected points are available
    (the loose noise floor used by the marker pipeline).
    """
    if pts_xy is None or len(pts_xy) < 10:
        return None

    pts = pts_xy.reshape(-1, 1, 2).astype(np.float32)
    plane = cv2.perspectiveTransform(pts, H_img_to_plane.astype(np.float32)).reshape(
        -1, 2
    )

    ok = np.isfinite(plane).all(axis=1)
    plane = plane[ok]
    if len(plane) < 10:
        return None

    mx = float(np.median(plane[:, 0]))
    my = float(np.median(plane[:, 1]))
    return mx, my


def project_to_ground_plane(
    H_prev_to_plane: np.ndarray,
    H_prev_to_cur: np.ndarray,
    p1_inliers: np.ndarray,
    fallback_xy_px: tuple[float, float],
) -> tuple[np.ndarray | None, tuple[float, float] | None]:
    """Compose plane homography and project inliers (with fallback point).

    Returns ``(H_cur_to_plane, xy_on_plane)``. ``xy_on_plane`` is the
    per-axis median of projected inliers, or the fallback pixel projected
    through ``H_cur_to_plane`` if the inlier set is too small. Returns
    ``(None, None)`` if the composed plane homography is degenerate.
    """
    invH = safe_inv_homography(H_prev_to_cur)
    if invH is None:
        return None, None
    H_cur_to_plane = (H_prev_to_plane @ invH).astype(np.float64)
    xy = project_points_median(H_cur_to_plane, p1_inliers)
    if xy is None:
        xy = project_point(H_cur_to_plane, fallback_xy_px[0], fallback_xy_px[1])
    return H_cur_to_plane, xy


# ── Geometry helpers ────────────────────────────────────────────────


def order_points(pts4: Sequence[np.ndarray]) -> np.ndarray:
    """Return the four points in TL, TR, BR, BL order."""
    pts = np.array(pts4, dtype=np.float32)
    s = pts.sum(axis=1)
    d = pts[:, 0] - pts[:, 1]
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmax(d)]
    bl = pts[np.argmin(d)]
    return np.array([tl, tr, br, bl], dtype=np.float32)


def polygon_area(pts4: Sequence[np.ndarray] | np.ndarray) -> float:
    """Polygon area for the 4-corner array."""
    arr = np.array(pts4, dtype=np.float32).reshape(-1, 2)
    x = arr[:, 0]
    y = arr[:, 1]
    area = 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))
    return float(area)


# ── LK + RANSAC homography ──────────────────────────────────────────


@dataclass
class LKTrackResult:
    """Output of one LK+RANSAC step between consecutive frames."""

    H_prev_to_cur: np.ndarray | None
    inlier_mask: np.ndarray | None
    p0_good: np.ndarray  # (N, 2) — filtered prev-frame points
    p1_good: np.ndarray  # (N, 2) — filtered cur-frame points
    force_redetect: bool


def lk_ransac_homography(
    prev_gray: np.ndarray,
    cur_gray: np.ndarray,
    prev_pts: np.ndarray,
    lk_params: dict,
    config: NavigationTuning,
    H_guess_prev_to_cur: np.ndarray | None = None,
) -> LKTrackResult:
    """Forward/backward LK + RANSAC homography between two gray frames.

    Mirrors the upstream marker-tracker:

    * Forward LK (with optional ``H_guess`` initial flow).
    * Forward error gate (``LK_ERR_THR_MARKER``).
    * Backward LK + forward-backward distance gate (``FB_THR_MARKER``).
    * RANSAC homography on the surviving matches.

    The returned ``force_redetect`` flag is True when the inlier ratio
    falls below ``HARD_RESET_RATIO_MARKER`` or the filtered match count
    drops under ``REDETECT_MIN_PTS_MARKER``.
    """
    empty = np.empty((0, 2), dtype=np.float32)
    result = LKTrackResult(
        H_prev_to_cur=None,
        inlier_mask=None,
        p0_good=empty,
        p1_good=empty,
        force_redetect=False,
    )

    lk_flags = 0
    next_init = None
    if (
        H_guess_prev_to_cur is not None
        and H_guess_prev_to_cur.shape == (3, 3)
        and np.isfinite(H_guess_prev_to_cur).all()
    ):
        next_init = cv2.perspectiveTransform(prev_pts, H_guess_prev_to_cur)
        lk_flags |= cv2.OPTFLOW_USE_INITIAL_FLOW

    cv2_any: Any = cv2
    fwd = cv2_any.calcOpticalFlowPyrLK(
        prev_gray, cur_gray, prev_pts, next_init, flags=lk_flags, **lk_params
    )
    cur_pts, st_fwd, err_fwd = fwd
    if cur_pts is None or st_fwd is None:
        return result

    st_fwd = st_fwd.reshape(-1).astype(bool)
    if np.count_nonzero(st_fwd) < config.min_track_pts_marker:
        return result

    p0_f = prev_pts[st_fwd]
    p1_f = cur_pts[st_fwd]
    good = np.ones((len(p0_f),), dtype=bool)

    if err_fwd is not None and config.lk_err_thr_marker > 0:
        ef = err_fwd.reshape(-1)[st_fwd]
        good &= np.isfinite(ef) & (ef < config.lk_err_thr_marker)

    if config.fb_thr_marker > 0:
        bk = cv2_any.calcOpticalFlowPyrLK(cur_gray, prev_gray, p1_f, None, **lk_params)
        back_pts, st_back, err_back = bk
        if back_pts is not None and st_back is not None:
            st_back = st_back.reshape(-1).astype(bool)
            good &= st_back
            fb = np.linalg.norm(p0_f.reshape(-1, 2) - back_pts.reshape(-1, 2), axis=1)
            good &= np.isfinite(fb) & (fb < config.fb_thr_marker)
            if err_back is not None and config.lk_err_thr_marker > 0:
                eb = err_back.reshape(-1)
                good &= np.isfinite(eb) & (eb < config.lk_err_thr_marker)

    p0 = p0_f.reshape(-1, 2)[good]
    p1 = p1_f.reshape(-1, 2)[good]

    if config.redetect_min_pts_marker > 0 and len(p0) < config.redetect_min_pts_marker:
        result.force_redetect = True

    if len(p0) < config.min_track_pts_marker:
        result.p0_good = p0
        result.p1_good = p1
        return result

    H_prev_to_cur, inl = cv2.findHomography(
        p0.reshape(-1, 1, 2),
        p1.reshape(-1, 1, 2),
        cv2.RANSAC,
        config.ransac_thr_marker,
    )

    result.p0_good = p0
    result.p1_good = p1
    result.H_prev_to_cur = H_prev_to_cur
    result.inlier_mask = inl

    if H_prev_to_cur is not None and inl is not None:
        inliers_cnt = int(inl.sum())
        ratio = float(inliers_cnt) / float(max(1, len(p0)))
        if ratio < config.hard_reset_ratio_marker:
            result.force_redetect = True

    return result
