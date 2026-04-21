"""Unit tests for pure navigation functions (homography, smoothing, optical flow)."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from rescue_ai.navigation.homography import (
    make_roi_mask,
    order_points,
    polygon_area,
    preprocess_gray,
    project_point,
    project_points_median,
    safe_inv_homography,
)
from rescue_ai.navigation.optical_flow import compute_scale_from_samples
from rescue_ai.navigation.smoothing import laplacian_smooth_last


def test_order_points_returns_tl_tr_br_bl() -> None:
    shuffled = [
        np.array([100.0, 100.0]),  # BR
        np.array([0.0, 0.0]),  # TL
        np.array([100.0, 0.0]),  # TR
        np.array([0.0, 100.0]),  # BL
    ]
    ordered = order_points(shuffled)
    assert ordered.shape == (4, 2)
    assert tuple(ordered[0]) == (0.0, 0.0)
    assert tuple(ordered[1]) == (100.0, 0.0)
    assert tuple(ordered[2]) == (100.0, 100.0)
    assert tuple(ordered[3]) == (0.0, 100.0)


def test_polygon_area_square() -> None:
    square = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]])
    assert polygon_area(square) == pytest.approx(100.0)


def test_make_roi_mask_bottom_half() -> None:
    gray = np.zeros((100, 50), dtype=np.uint8)
    mask = make_roi_mask(gray, roi_top_ratio=0.5)
    assert mask.shape == gray.shape
    assert mask[0:50, :].sum() == 0
    assert (mask[50:, :] == 255).all()


def test_preprocess_gray_shape_and_dtype() -> None:
    bgr = np.zeros((20, 30, 3), dtype=np.uint8)
    gray = preprocess_gray(bgr)
    assert gray.shape == (20, 30)
    assert gray.dtype == np.uint8


def test_safe_inv_homography_rejects_singular() -> None:
    H = np.eye(3)
    H[2, 2] = 0.0
    assert safe_inv_homography(H) is None


def test_safe_inv_homography_normalises_h22() -> None:
    H = np.eye(3) * 2.0
    invH = safe_inv_homography(H)
    assert invH is not None
    assert invH[2, 2] == pytest.approx(1.0)


def test_project_point_identity() -> None:
    H = np.eye(3)
    assert project_point(H, 3.0, 4.0) == (3.0, 4.0)


def test_project_points_median_requires_ten_points() -> None:
    H = np.eye(3)
    pts = np.random.default_rng(0).uniform(0.0, 10.0, size=(9, 2)).astype(np.float32)
    assert project_points_median(H, pts) is None


def test_project_points_median_returns_median() -> None:
    H = np.eye(3)
    pts = np.array([[float(i), float(i)] for i in range(10)], dtype=np.float32)
    result = project_points_median(H, pts)
    assert result is not None
    mx, my = result
    assert mx == pytest.approx(4.5)
    assert my == pytest.approx(4.5)


def test_compute_scale_from_samples_none_if_empty() -> None:
    assert compute_scale_from_samples(None) is None
    assert compute_scale_from_samples(np.empty((0, 2))) is None


def test_compute_scale_from_samples_symmetric_cloud() -> None:
    pts = np.array([[1.0, 0.0], [-1.0, 0.0], [0.0, 1.0], [0.0, -1.0]])
    assert compute_scale_from_samples(pts) == pytest.approx(1.0)


def test_laplacian_smooth_last_no_op_when_too_few_points() -> None:
    pos = np.array([10.0, 10.0, 5.0])
    assert np.array_equal(laplacian_smooth_last([], pos), pos)
    assert np.array_equal(laplacian_smooth_last([np.array([0.0, 0.0, 0.0])], pos), pos)


def test_laplacian_smooth_last_reduces_large_jump() -> None:
    traj = [np.array([0.0, 0.0, 0.0]), np.array([1.0, 1.0, 0.0])]
    pos = np.array([10.0, 10.0, 5.0])
    smoothed = laplacian_smooth_last(traj, pos)
    assert np.linalg.norm(smoothed - pos) > 0.0
    assert smoothed[0] < pos[0]
    assert smoothed[1] < pos[1]


def test_laplacian_smooth_last_pulls_endpoint_toward_neighbor() -> None:
    # Endpoint of a path graph has degree 1 — smoothing pulls it toward
    # its single neighbor with magnitude ~ 2 * lr * unit_step.
    traj = [np.array([float(i), 0.0, 0.0]) for i in range(5)]
    pos = np.array([5.0, 0.0, 0.0])
    smoothed = laplacian_smooth_last(traj, pos)
    # x decreases (pulled back toward neighbor at x=4); y/z untouched.
    assert smoothed[0] < pos[0]
    assert smoothed[1] == pytest.approx(0.0)
    assert smoothed[2] == pytest.approx(0.0)


def test_cv2_is_available_so_tests_run_in_ci() -> None:
    assert hasattr(cv2, "findHomography")
