"""Pure homography utilities for the marker pipeline."""

from __future__ import annotations

from typing import Sequence

import cv2
import numpy as np


def safe_inv_homography(  # pylint: disable=too-many-return-statements
    H: np.ndarray | None,
) -> np.ndarray | None:
    """Invert a 3x3 homography with conditioning + finite-value guards.

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
    # pylint: disable-next=too-many-function-args
    arr = np.array(pts4, dtype=np.float32).reshape(-1, 1, 2)
    return float(abs(cv2.contourArea(arr)))


def make_roi_mask(gray: np.ndarray, roi_top_ratio: float = 0.45) -> np.ndarray:
    """Return a binary mask covering only the bottom (1 - roi_top_ratio) part."""
    h, w = gray.shape
    m = np.zeros((h, w), dtype=np.uint8)
    m[int(h * roi_top_ratio) :, :] = 255  # noqa: E203
    return m


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
