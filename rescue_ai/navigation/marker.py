"""Red-marker detection and PnP-based pose estimation."""

from __future__ import annotations

import cv2
import numpy as np

from rescue_ai.navigation.tuning import NavigationTuning

# Unit square centred at origin — scaled by marker_size_m at call site.
MARKER_3D = np.array(
    [[-0.5, -0.5, 0], [0.5, -0.5, 0], [0.5, 0.5, 0], [-0.5, 0.5, 0]],
    dtype=np.float32,
)


def build_marker_intrinsics(config: NavigationTuning) -> np.ndarray:
    """3×3 intrinsics scaled from reference resolution to marker working size."""
    fx = config.fx_nav * (float(config.marker_resize_w) / float(config.nav_width))
    fy = config.fy_nav * (float(config.marker_resize_h) / float(config.nav_height))
    return np.array(
        [
            [fx, 0.0, float(config.marker_resize_w) / 2.0],
            [0.0, fy, float(config.marker_resize_h) / 2.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )


def detect_red_square_corners(frame_bgr: np.ndarray) -> np.ndarray | None:
    """Detect a red square by HSV mask + 4-corner approximation.

    Returns the 4 corners ordered by angle around the centroid, or None
    if no suitable contour is found.
    """
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    lower1 = np.array((0, 80, 80), dtype=np.uint8)
    upper1 = np.array((10, 255, 255), dtype=np.uint8)
    lower2 = np.array((170, 80, 80), dtype=np.uint8)
    upper2 = np.array((180, 255, 255), dtype=np.uint8)
    mask1 = cv2.inRange(hsv, lower1, upper1)
    mask2 = cv2.inRange(hsv, lower2, upper2)
    mask = cv2.bitwise_or(mask1, mask2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    cnt = max(cnts, key=cv2.contourArea)
    if cv2.contourArea(cnt) < 1500:
        return None
    approx = cv2.approxPolyDP(cnt, 0.03 * cv2.arcLength(cnt, True), True)
    if len(approx) != 4:
        return None
    pts = approx.reshape(4, 2).astype(np.float32)
    c = pts.mean(axis=0)
    ang = np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0])
    return pts[np.argsort(ang)]


def detect_red_square_corners_alt(frame_bgr: np.ndarray) -> np.ndarray | None:
    """Alternative red-square detector — used by the altitude branch.

    Kept as a separate function to match upstream behaviour byte-exactly
    (the altitude branch and the tracker each have their own detector
    with slightly different morphology kernels).
    """
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    lower1 = np.array((0, 80, 80), dtype=np.uint8)
    upper1 = np.array((10, 255, 255), dtype=np.uint8)
    lower2 = np.array((170, 80, 80), dtype=np.uint8)
    upper2 = np.array((180, 255, 255), dtype=np.uint8)
    mask1 = cv2.inRange(hsv, lower1, upper1)
    mask2 = cv2.inRange(hsv, lower2, upper2)
    mask = cv2.bitwise_or(mask1, mask2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((7, 7), dtype=np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), dtype=np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    cnt = max(cnts, key=cv2.contourArea)
    if cv2.contourArea(cnt) < 1500:
        return None
    approx = cv2.approxPolyDP(cnt, 0.03 * cv2.arcLength(cnt, True), True)
    if len(approx) != 4:
        return None
    pts = approx.reshape(4, 2).astype(np.float32)
    c = pts.mean(axis=0)
    ang = np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0])
    return pts[np.argsort(ang)]


def detect_red_marker_corners(
    frame_bgr: np.ndarray,
) -> tuple[np.ndarray | None, np.ndarray]:
    """Robust red-marker detector returning (corners4, mask).

    Uses connected-components + goodFeaturesToTrack on Canny edges to
    find the four marker corners. Falls back to mask centroids if not
    enough corners are found. Returns (None, mask) when the area is too
    small or detected corners fall outside the dilated mask.
    """
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)

    lower1 = np.array([0, 120, 60], dtype=np.uint8)
    upper1 = np.array([10, 255, 255], dtype=np.uint8)
    lower2 = np.array([170, 120, 60], dtype=np.uint8)
    upper2 = np.array([180, 255, 255], dtype=np.uint8)

    mask = cv2.bitwise_or(
        cv2.inRange(hsv, lower1, upper1), cv2.inRange(hsv, lower2, upper2)
    )

    k = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=1)

    num, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if num <= 1:
        return None, mask

    largest = 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])
    area = int(stats[largest, cv2.CC_STAT_AREA])
    if area < 2500:
        return None, mask

    mask = (labels == largest).astype(np.uint8) * 255

    edges = cv2.Canny(mask, 50, 150)
    corners = cv2.goodFeaturesToTrack(
        edges, maxCorners=160, qualityLevel=0.01, minDistance=18, blockSize=7
    )

    if corners is None or len(corners) < 4:
        ys, xs = np.where(mask == 255)
        if len(xs) < 2000:
            return None, mask
        pts = np.stack([xs, ys], axis=1).astype(np.float32)
    else:
        pts = corners.reshape(-1, 2).astype(np.float32)

    from rescue_ai.navigation.tracking import order_points

    pts4 = order_points(
        [
            pts[np.argmin(pts[:, 0] + pts[:, 1])],
            pts[np.argmax(pts[:, 0] - pts[:, 1])],
            pts[np.argmax(pts[:, 0] + pts[:, 1])],
            pts[np.argmin(pts[:, 0] - pts[:, 1])],
        ]
    )

    dil = cv2.dilate(mask, np.ones((9, 9), np.uint8), iterations=1)
    h, w = mask.shape
    for p in pts4:
        x, y = int(round(p[0])), int(round(p[1]))
        x = max(0, min(w - 1, x))
        y = max(0, min(h - 1, y))
        if dil[y, x] == 0:
            return None, mask

    return pts4, mask


def estimate_marker_pose_pnp(
    corners_px: np.ndarray, K: np.ndarray, marker_size_m: float = 1.0
) -> np.ndarray | None:
    """PnP-based camera centre estimate from 4 marker corners.

    Returns ``[tx, ty, |tz|]`` in metres (camera centre in marker frame),
    or None if solvePnP fails.
    """
    marker_3d = MARKER_3D * float(marker_size_m)
    ok, _, tvec = cv2.solvePnP(
        marker_3d, corners_px, K, None, flags=cv2.SOLVEPNP_IPPE_SQUARE
    )
    if not ok:
        return None
    t = tvec.flatten()
    return np.array([t[0], t[1], abs(t[2])], dtype=float)
