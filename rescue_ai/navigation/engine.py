"""Navigation engine implementations for automatic-mode missions.

Implements ``NavigationEnginePort`` from the domain layer. This module
holds the marker-based engine ported from diplom-prod's
``run_unified_pipeline``. The no-marker engine is deferred to P1.2.5.

Design constraints:
* No I/O — frames are supplied by the caller, poses returned synchronously.
* Pure-Python state, deterministic given the same input frames.
* Math kept byte-identical to the upstream pipeline (regression-checked
  against the golden trajectory CSV in env-gated tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

import cv2
import numpy as np

from rescue_ai.domain.entities import TrajectoryPoint
from rescue_ai.domain.value_objects import TrajectorySource
from rescue_ai.navigation.constants import (
    AUTO_MARKER_SECONDS,
    FB_THR_MARKER,
    FLIP_X_MARKER,
    FLIP_Y_MARKER,
    GFTT_BLOCK,
    GFTT_MIN_DIST,
    GFTT_QUALITY,
    HARD_RESET_RATIO_MARKER,
    LK_ERR_THR_MARKER,
    LK_LEVELS_MARKER,
    LK_WIN_MARKER,
    MARKER_ALPHA_ALT,
    MARKER_AREA_MIN,
    MARKER_CHECK_EVERY,
    MARKER_RESIZE_H,
    MARKER_RESIZE_W,
    MARKER_SIZE_XY_M,
    MAX_CORNERS_MARKER,
    MAX_SPEED_MARKER,
    MAX_STEP_SCALE_MARKER,
    MIN_INLIER_RATIO_MARKER,
    MIN_INLIERS_MARKER,
    MIN_TRACK_PTS_MARKER,
    RANSAC_THR_MARKER,
    REDETECT_MIN_PTS_MARKER,
    ROI_TOP_RATIO,
    SMOOTH_JUMP_XY,
    SMOOTH_JUMP_Z,
)
from rescue_ai.navigation.homography import (
    make_roi_mask,
    polygon_area,
    preprocess_gray,
    project_point,
    project_points_median,
    safe_inv_homography,
)
from rescue_ai.navigation.marker_pose import (
    detect_red_marker_corners,
    detect_red_square_corners_alt,
    estimate_camera_center_from_marker,
)
from rescue_ai.navigation.optical_flow import (
    compute_scale_from_samples,
    update_altitude_from_flow,
)
from rescue_ai.navigation.smoothing import laplacian_smooth_last


@dataclass
class MarkerEngineConfig:
    """Marker-engine tuning. Defaults match diplom-prod."""

    fps: float = 30.0
    auto_marker_seconds: float = AUTO_MARKER_SECONDS
    marker_size_xy_m: float = MARKER_SIZE_XY_M
    flip_x: bool = FLIP_X_MARKER
    flip_y: bool = FLIP_Y_MARKER
    initial_altitude_m: float = 1.5


_DST_MARKER_NAV_M = np.array(
    [
        [-0.5, -0.5],
        [0.5, -0.5],
        [0.5, 0.5],
        [-0.5, 0.5],
    ],
    dtype=np.float32,
)


def _build_intrinsics(width: int, height: int) -> np.ndarray:
    fx = 1100.0 * (float(width) / 853.0)
    fy = 1100.0 * (float(height) / 480.0)
    return np.array(
        [
            [fx, 0.0, float(width) / 2.0],
            [0.0, fy, float(height) / 2.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )


@dataclass
class _MarkerEngineState:
    """Internal mutable state of a running marker engine."""

    initialised: bool = False
    init_buffer: list[tuple[int | None, float, np.ndarray]] = field(
        default_factory=list
    )
    seq: int = 0
    pos: np.ndarray = field(default_factory=lambda: np.zeros(3, dtype=float))
    last_xy: tuple[float, float] = (0.0, 0.0)
    last_accept_t_abs: float = 0.0
    track_x: float = 0.0
    track_y: float = 0.0
    H_prev_to_plane: np.ndarray | None = None
    H_guess_prev_to_cur: np.ndarray | None = None
    prev_gray_marker: np.ndarray | None = None
    prev_roi_marker: np.ndarray | None = None
    prev_pts_marker: np.ndarray | None = None
    alt_agl_state: float = 1.5
    alt_prev_gray: np.ndarray | None = None
    alt_prev_pts: np.ndarray | None = None
    alt_prev_scale: float | None = None
    alt_marker_seen: bool = True
    alt_marker_cnt: int = 0
    K_alt: np.ndarray = field(
        default_factory=lambda: _build_intrinsics(MARKER_RESIZE_W, MARKER_RESIZE_H)
    )
    processed: int = 0
    traj_points: list[np.ndarray] = field(default_factory=list)
    mission_id: str = ""


class MarkerEngine:
    """Marker-based navigation engine implementing ``NavigationEnginePort``.

    Lifecycle:
        engine.reset()  → start a fresh trajectory
        engine.step(frame_bgr, ts_sec, frame_id) → TrajectoryPoint | None

    The first ``auto_marker_seconds * fps`` frames are buffered; once the
    buffer is full the engine picks the best marker frame as the origin and
    starts emitting points (the very first emitted point has ``seq=0``).
    Subsequent frames produce one point each — accepted by the LK+RANSAC
    homography gate, or carried forward from the previous accepted pose.
    """

    def __init__(
        self,
        mission_id: str,
        config: MarkerEngineConfig | None = None,
    ) -> None:
        self._mission_id = mission_id
        self._config = config or MarkerEngineConfig()
        self._state = _MarkerEngineState(mission_id=mission_id)
        self._state.alt_agl_state = self._config.initial_altitude_m
        self._lk_alt = {
            "winSize": (21, 21),
            "maxLevel": 3,
            "criteria": (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
        }
        lk_win = LK_WIN_MARKER if (LK_WIN_MARKER % 2 == 1) else LK_WIN_MARKER + 1
        self._lk_params = {
            "winSize": (lk_win, lk_win),
            "maxLevel": LK_LEVELS_MARKER,
            "criteria": (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
        }

    # ── NavigationEnginePort ────────────────────────────────────────

    def reset(self) -> None:
        self._state = _MarkerEngineState(mission_id=self._mission_id)
        self._state.alt_agl_state = self._config.initial_altitude_m

    def step(
        self,
        frame_bgr: object,
        ts_sec: float,
        frame_id: int | None = None,
    ) -> TrajectoryPoint | None:
        frame = np.asarray(frame_bgr)
        if frame.ndim != 3 or frame.shape[2] != 3:
            return None
        frame_marker = cv2.resize(frame, (MARKER_RESIZE_W, MARKER_RESIZE_H))

        if not self._state.initialised:
            return self._collect_init_frame(frame_marker, ts_sec, frame_id)

        return self._process_frame(frame_marker, ts_sec, frame_id)

    # ── init phase ──────────────────────────────────────────────────

    def _collect_init_frame(  # pylint: disable=too-many-locals
        self, frame_marker: np.ndarray, ts_sec: float, frame_id: int | None
    ) -> TrajectoryPoint | None:
        max_init = max(1, int(self._config.auto_marker_seconds * self._config.fps))
        self._state.init_buffer.append((frame_id, ts_sec, frame_marker.copy()))
        if len(self._state.init_buffer) < max_init:
            return None

        candidates: list[
            tuple[int, float, np.ndarray, int | None, float, np.ndarray]
        ] = []
        for idx, (fid, ts, fm) in enumerate(self._state.init_buffer):
            pts, _ = detect_red_marker_corners(fm)
            if pts is None:
                continue
            area = polygon_area(pts)
            if 3000 < area < (MARKER_RESIZE_W * MARKER_RESIZE_H) * 0.5:
                candidates.append((idx, area, pts, fid, ts, fm))

        if not candidates:
            self._state.init_buffer.clear()
            return None

        candidates.sort(key=lambda x: x[1], reverse=True)
        topk = candidates[: min(40, len(candidates))]
        stack = np.stack([c[2] for c in topk], axis=0)
        median_corners = np.median(stack, axis=0)
        best = min(topk, key=lambda c: float(np.linalg.norm(c[2] - median_corners)))
        _, _, best_pts4, best_fid, best_ts, best_frame = best

        H_init = cv2.getPerspectiveTransform(
            best_pts4.astype(np.float32), _DST_MARKER_NAV_M
        ).astype(np.float64)

        s = self._state
        s.H_prev_to_plane = H_init
        s.prev_gray_marker = preprocess_gray(best_frame)
        s.prev_roi_marker = make_roi_mask(s.prev_gray_marker, ROI_TOP_RATIO)
        s.prev_pts_marker = self._detect_features(s.prev_gray_marker, s.prev_roi_marker)
        s.track_x = 0.5 * (MARKER_RESIZE_W - 1)
        s.track_y = 0.85 * (MARKER_RESIZE_H - 1)

        xy = project_point(H_init, s.track_x, s.track_y)
        s.last_xy = xy if xy is not None else (0.0, 0.0)
        s.last_accept_t_abs = float(best_ts)

        s.alt_prev_gray = cv2.cvtColor(best_frame, cv2.COLOR_BGR2GRAY)
        s.alt_prev_pts = cv2.goodFeaturesToTrack(s.alt_prev_gray, 500, 0.01, 7)
        s.alt_prev_scale = (
            compute_scale_from_samples(s.alt_prev_pts.reshape(-1, 2))
            if s.alt_prev_pts is not None
            else None
        )

        cam_pos = estimate_camera_center_from_marker(
            best_pts4.astype(np.float32), s.K_alt, 1.0
        )
        if cam_pos is not None and np.isfinite(cam_pos).all():
            s.alt_agl_state = float(cam_pos[2])

        x_nav = -s.last_xy[0] if self._config.flip_x else s.last_xy[0]
        y_nav = -s.last_xy[1] if self._config.flip_y else s.last_xy[1]
        s.pos = np.array(
            [
                float(x_nav * self._config.marker_size_xy_m),
                float(y_nav * self._config.marker_size_xy_m),
                float(s.alt_agl_state),
            ],
            dtype=float,
        )
        s.traj_points = [s.pos.copy()]
        s.initialised = True
        s.init_buffer.clear()
        s.seq = 0

        return TrajectoryPoint(
            mission_id=self._mission_id,
            seq=0,
            ts_sec=float(best_ts),
            x=float(s.pos[0]),
            y=float(s.pos[1]),
            z=float(s.pos[2]),
            source=TrajectorySource.MARKER,
            frame_id=best_fid,
        )

    # ── per-frame processing ────────────────────────────────────────

    def _process_frame(  # noqa: E501  # pylint: disable=too-many-locals,too-many-branches,too-many-statements,too-many-nested-blocks
        self, frame_marker: np.ndarray, ts_sec: float, frame_id: int | None
    ) -> TrajectoryPoint | None:
        s = self._state
        s.processed += 1
        cur_gray = preprocess_gray(frame_marker)
        cur_roi = make_roi_mask(cur_gray, ROI_TOP_RATIO)
        alt_gray = cv2.cvtColor(frame_marker, cv2.COLOR_BGR2GRAY)

        # Altitude branch (PnP marker first, fallback to scale-from-flow).
        alt_marker = detect_red_square_corners_alt(frame_marker)
        if alt_marker is not None and polygon_area(alt_marker) > MARKER_AREA_MIN:
            s.alt_marker_seen = True
            s.alt_marker_cnt += 1
            cam_pos = estimate_camera_center_from_marker(
                alt_marker.astype(np.float32), s.K_alt, 1.0
            )
            if cam_pos is not None and np.isfinite(cam_pos).all():
                s.alt_agl_state = (1.0 - MARKER_ALPHA_ALT) * s.alt_agl_state + (
                    MARKER_ALPHA_ALT * float(cam_pos[2])
                )
            s.alt_prev_pts = cv2.goodFeaturesToTrack(alt_gray, 500, 0.01, 7)
            s.alt_prev_scale = (
                compute_scale_from_samples(s.alt_prev_pts.reshape(-1, 2))
                if s.alt_prev_pts is not None
                else None
            )
        else:
            if s.alt_marker_seen and s.alt_marker_cnt > 10:
                s.alt_marker_seen = False
            if (
                (not s.alt_marker_seen)
                and s.alt_prev_pts is not None
                and s.alt_prev_gray is not None
            ):
                upd = update_altitude_from_flow(
                    prev_gray=s.alt_prev_gray,
                    cur_gray=alt_gray,
                    prev_pts=s.alt_prev_pts,
                    prev_scale=s.alt_prev_scale,
                    current_alt=s.alt_agl_state,
                    lk_params=self._lk_alt,
                )
                s.alt_agl_state = upd.altitude
                s.alt_prev_pts = upd.next_pts
                s.alt_prev_scale = upd.next_scale
            else:
                s.alt_prev_pts = cv2.goodFeaturesToTrack(alt_gray, 500, 0.01, 7)
        s.alt_prev_gray = alt_gray

        # Periodic direct-detect reset.
        force_redetect_next = False
        reset = 0
        if MARKER_CHECK_EVERY > 0 and (s.processed % MARKER_CHECK_EVERY == 0):
            pts_m, _ = detect_red_marker_corners(frame_marker)
            if pts_m is not None and polygon_area(pts_m) > MARKER_AREA_MIN:
                H_direct = cv2.getPerspectiveTransform(
                    pts_m.astype(np.float32), _DST_MARKER_NAV_M
                ).astype(np.float64)
                xy_direct = project_point(H_direct, s.track_x, s.track_y)
                if xy_direct is not None:
                    dist = float(
                        np.hypot(
                            xy_direct[0] - s.last_xy[0],
                            xy_direct[1] - s.last_xy[1],
                        )
                    )
                    if dist < 30.0:
                        s.H_prev_to_plane = H_direct
                        reset = 1
                        s.last_xy = xy_direct
                        s.last_accept_t_abs = ts_sec

        # LK + RANSAC homography prev → cur.
        accepted = False
        if s.prev_pts_marker is None or len(s.prev_pts_marker) < MIN_TRACK_PTS_MARKER:
            s.prev_pts_marker = self._detect_features(
                s.prev_gray_marker, s.prev_roi_marker
            )

        if (
            s.prev_pts_marker is not None
            and len(s.prev_pts_marker) >= MIN_TRACK_PTS_MARKER
            and s.prev_gray_marker is not None
        ):
            lk_flags = 0
            next_init = None
            if s.H_guess_prev_to_cur is not None:
                try:
                    next_init = cv2.perspectiveTransform(
                        s.prev_pts_marker, s.H_guess_prev_to_cur
                    )
                    lk_flags |= cv2.OPTFLOW_USE_INITIAL_FLOW
                except cv2.error:  # pylint: disable=catching-non-exception
                    next_init = None
                    lk_flags = 0

            lk_out = cv2.calcOpticalFlowPyrLK(  # type: ignore[call-overload]
                s.prev_gray_marker,
                cur_gray,
                s.prev_pts_marker,
                next_init,
                flags=lk_flags,
                **self._lk_params,
            )
            cur_pts, st_fwd, err_fwd = lk_out
            if cur_pts is not None and st_fwd is not None:
                st_fwd = st_fwd.reshape(-1).astype(bool)
                p0 = np.empty((0, 2), dtype=np.float32)
                p1 = np.empty((0, 2), dtype=np.float32)

                if np.count_nonzero(st_fwd) >= MIN_TRACK_PTS_MARKER:
                    p0_f = s.prev_pts_marker[st_fwd]
                    p1_f = cur_pts[st_fwd]
                    good = np.ones((len(p0_f),), dtype=bool)

                    if err_fwd is not None and LK_ERR_THR_MARKER > 0:
                        ef = err_fwd.reshape(-1)[st_fwd]
                        good &= np.isfinite(ef) & (ef < LK_ERR_THR_MARKER)

                    if FB_THR_MARKER > 0:
                        bk_out = cv2.calcOpticalFlowPyrLK(  # type: ignore[call-overload] # noqa: E501  # pylint: disable=line-too-long
                            cur_gray,
                            s.prev_gray_marker,
                            p1_f,
                            None,
                            **self._lk_params,
                        )
                        back_pts, st_back, err_back = bk_out
                        if back_pts is not None and st_back is not None:
                            st_back = st_back.reshape(-1).astype(bool)
                            good &= st_back
                            fb = np.linalg.norm(
                                p0_f.reshape(-1, 2) - back_pts.reshape(-1, 2), axis=1
                            )
                            good &= np.isfinite(fb) & (fb < FB_THR_MARKER)
                            if err_back is not None and LK_ERR_THR_MARKER > 0:
                                eb = err_back.reshape(-1)
                                good &= np.isfinite(eb) & (eb < LK_ERR_THR_MARKER)

                    p0 = p0_f.reshape(-1, 2)[good]
                    p1 = p1_f.reshape(-1, 2)[good]

                    if (
                        REDETECT_MIN_PTS_MARKER > 0
                        and len(p0) < REDETECT_MIN_PTS_MARKER
                    ):
                        force_redetect_next = True

                if len(p0) >= MIN_TRACK_PTS_MARKER:
                    H_prev_to_cur, inl = cv2.findHomography(
                        p0.reshape(-1, 1, 2),
                        p1.reshape(-1, 1, 2),
                        cv2.RANSAC,
                        RANSAC_THR_MARKER,
                    )
                    if H_prev_to_cur is not None and inl is not None:
                        inliers_cnt = int(inl.sum())
                        ratio = float(inliers_cnt) / float(max(1, len(p0)))
                        if (
                            inliers_cnt >= MIN_INLIERS_MARKER
                            and ratio >= MIN_INLIER_RATIO_MARKER
                        ):
                            s.H_guess_prev_to_cur = H_prev_to_cur
                        min_inl_dyn = max(MIN_INLIERS_MARKER, int(0.30 * len(p0)))
                        if ratio < HARD_RESET_RATIO_MARKER:
                            force_redetect_next = True

                        if (
                            inliers_cnt >= min_inl_dyn
                            and ratio >= MIN_INLIER_RATIO_MARKER
                            and s.H_prev_to_plane is not None
                        ):
                            invH = safe_inv_homography(H_prev_to_cur)
                            if invH is not None:
                                H_cur_to_plane = (s.H_prev_to_plane @ invH).astype(
                                    np.float64
                                )
                                inl_mask = inl.reshape(-1).astype(bool)
                                p1_in = p1[inl_mask]
                                xy_candidate = project_points_median(
                                    H_cur_to_plane, p1_in
                                )
                                if xy_candidate is None:
                                    xy_candidate = project_point(
                                        H_cur_to_plane, s.track_x, s.track_y
                                    )
                                if xy_candidate is not None:
                                    dt = float(ts_sec - s.last_accept_t_abs)
                                    if dt <= 0:
                                        dt = 1.0 / max(float(self._config.fps), 1e-6)
                                    step = float(
                                        np.hypot(
                                            xy_candidate[0] - s.last_xy[0],
                                            xy_candidate[1] - s.last_xy[1],
                                        )
                                    )
                                    max_step = (
                                        MAX_SPEED_MARKER
                                        * dt
                                        * float(MAX_STEP_SCALE_MARKER)
                                    )
                                    if reset == 1 or step <= max_step:
                                        accepted = True
                                        s.last_accept_t_abs = float(ts_sec)
                                        s.H_prev_to_plane = H_cur_to_plane
                                        s.last_xy = (
                                            float(xy_candidate[0]),
                                            float(xy_candidate[1]),
                                        )
                                        s.prev_pts_marker = p1_in.reshape(
                                            -1, 1, 2
                                        ).astype(np.float32)

        if not accepted:
            s.prev_pts_marker = self._detect_features(cur_gray, cur_roi)

        s.prev_gray_marker = cur_gray
        s.prev_roi_marker = cur_roi
        if force_redetect_next:
            s.prev_pts_marker = self._detect_features(
                s.prev_gray_marker, s.prev_roi_marker
            )

        x_nav = -s.last_xy[0] if self._config.flip_x else s.last_xy[0]
        y_nav = -s.last_xy[1] if self._config.flip_y else s.last_xy[1]
        new_pos = np.array(
            [
                float(x_nav * self._config.marker_size_xy_m),
                float(y_nav * self._config.marker_size_xy_m),
                float(s.alt_agl_state),
            ],
            dtype=float,
        )

        if s.traj_points:
            prev = s.traj_points[-1]
            jump_xy = float(np.linalg.norm(new_pos[:2] - prev[:2]))
            jump_z = float(abs(new_pos[2] - prev[2]))
            if jump_xy > SMOOTH_JUMP_XY or jump_z > SMOOTH_JUMP_Z:
                new_pos = laplacian_smooth_last(s.traj_points, new_pos)

        s.pos = new_pos
        s.seq += 1
        s.traj_points.append(new_pos.copy())

        return TrajectoryPoint(
            mission_id=self._mission_id,
            seq=s.seq,
            ts_sec=float(ts_sec),
            x=float(new_pos[0]),
            y=float(new_pos[1]),
            z=float(new_pos[2]),
            source=TrajectorySource.MARKER,
            frame_id=frame_id,
        )

    @staticmethod
    def _detect_features(
        gray: np.ndarray | None, mask: np.ndarray | None
    ) -> np.ndarray | None:
        if gray is None:
            return None
        return cv2.goodFeaturesToTrack(
            gray,
            maxCorners=MAX_CORNERS_MARKER,
            qualityLevel=GFTT_QUALITY,
            minDistance=GFTT_MIN_DIST,
            blockSize=GFTT_BLOCK,
            mask=mask,
        )


def new_engine(mission_id: str | None = None, fps: float = 30.0) -> MarkerEngine:
    """Convenience constructor with a default mission id."""
    return MarkerEngine(
        mission_id=mission_id or f"auto-{uuid4().hex[:8]}",
        config=MarkerEngineConfig(fps=fps),
    )
