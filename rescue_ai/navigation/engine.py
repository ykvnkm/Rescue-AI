"""Navigation engines for automatic-mode missions.

Three classes live here:

* ``MarkerEngine``    — marker-based pipeline (LK+RANSAC homography,
                         PnP altitude).
* ``NoMarkerEngine``  — fallback monocular odometry (sparse LK flow
                         with phase-correlation fallback, Laplacian
                         smoothing on detected jumps).
* ``NavigationEngine`` — public orchestrator implementing
                          ``NavigationEnginePort``. Buffers the first
                          ``auto_marker_seconds`` of frames, probes for
                          a marker, then delegates every subsequent
                          frame to one of the two engines above (the
                          choice is frozen after init).

Design constraints:
* No I/O — frames supplied by the caller, poses returned synchronously.
* Pure-Python state; deterministic given the same input frames.
* Math kept byte-identical to the upstream pipeline (regression-checked
  against the golden trajectory CSV in env-gated tests).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from uuid import uuid4

import cv2
import numpy as np

from rescue_ai.domain.entities import TrajectoryPoint
from rescue_ai.domain.value_objects import NavMode, TrajectorySource

logger = logging.getLogger(__name__)
from rescue_ai.navigation.altitude import (
    AltitudeUpdate,
    compute_scale_from_samples,
    ema_altitude_from_pnp,
    estimate_altitude_from_scale,
)
from rescue_ai.navigation.marker import (
    build_marker_intrinsics,
    detect_red_marker_corners,
    detect_red_square_corners_alt,
    estimate_marker_pose_pnp,
)
from rescue_ai.navigation.no_marker import optical_flow_lk_or_phase
from rescue_ai.navigation.smoothing import laplacian_smooth_window
from rescue_ai.navigation.tracking import (
    LKTrackResult,
    lk_ransac_homography,
    make_roi_mask,
    polygon_area,
    preprocess_gray,
    project_point,
    project_to_ground_plane,
)
from rescue_ai.navigation.tuning import NavigationTuning

_DST_MARKER_NAV_M = np.array(
    [
        [-0.5, -0.5],
        [0.5, -0.5],
        [0.5, 0.5],
        [-0.5, 0.5],
    ],
    dtype=np.float32,
)


def _build_lk_params(config: NavigationTuning) -> dict:
    lk_win = (
        config.lk_win_marker
        if config.lk_win_marker % 2 == 1
        else config.lk_win_marker + 1
    )
    return {
        "winSize": (lk_win, lk_win),
        "maxLevel": config.lk_levels_marker,
        "criteria": (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
    }


def _build_altitude_lk_params() -> dict:
    return {
        "winSize": (21, 21),
        "maxLevel": 3,
        "criteria": (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
    }


def _build_no_marker_lk_params() -> dict:
    return {
        "winSize": (21, 21),
        "maxLevel": 3,
        "criteria": (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
    }


# ════════════════════════════════════════════════════════════════════
# MarkerEngine
# ════════════════════════════════════════════════════════════════════


@dataclass
class _MarkerState:
    """Mutable state of a running marker engine (post-init)."""

    seq: int = 0
    track_x: float = 0.0
    track_y: float = 0.0
    last_xy: tuple[float, float] = (0.0, 0.0)
    last_accept_t_abs: float = 0.0
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
    processed: int = 0


@dataclass(frozen=True)
class _MarkerTrackOutcome:
    """State transition result for one marker LK/RANSAC step."""

    accepted: bool
    force_redetect: bool = False


class MarkerEngine:
    """Marker-based navigation engine.

    Fed one pre-resized marker frame per ``step`` call after the host has
    chosen the best initial marker pose. Emits ``TrajectoryPoint`` for
    every frame, carrying forward the previous accepted pose when the
    LK+RANSAC gate rejects the current frame.
    """

    def __init__(self, mission_id: str, config: NavigationTuning) -> None:
        self._mission_id = mission_id
        self._config = config
        self._k_alt = build_marker_intrinsics(config)
        self._lk_params = _build_lk_params(config)
        self._lk_alt = _build_altitude_lk_params()
        self._state = _MarkerState(alt_agl_state=config.initial_altitude_m)

    def seed_from_init(
        self,
        best_pts4: np.ndarray,
        best_frame_bgr: np.ndarray,
    ) -> tuple[float, float, float]:
        """Initialise state from the chosen init-frame marker.

        Returns the initial ``(x, y, z)`` in metric nav-frame coordinates.
        """
        s = self._state
        config = self._config

        H_init = cv2.getPerspectiveTransform(
            best_pts4.astype(np.float32), _DST_MARKER_NAV_M
        ).astype(np.float64)
        s.H_prev_to_plane = H_init
        s.prev_gray_marker = preprocess_gray(best_frame_bgr)
        s.prev_roi_marker = make_roi_mask(s.prev_gray_marker, config.roi_top_ratio)
        s.prev_pts_marker = self._detect_features(s.prev_gray_marker, s.prev_roi_marker)
        s.track_x = 0.5 * (config.marker_resize_w - 1)
        s.track_y = 0.85 * (config.marker_resize_h - 1)

        xy = project_point(H_init, s.track_x, s.track_y)
        s.last_xy = xy if xy is not None else (0.0, 0.0)

        s.alt_prev_gray = cv2.cvtColor(best_frame_bgr, cv2.COLOR_BGR2GRAY)
        s.alt_prev_pts = cv2.goodFeaturesToTrack(
            s.alt_prev_gray,
            config.alt_gftt_max_corners,
            config.alt_gftt_quality,
            config.alt_gftt_min_dist,
        )
        s.alt_prev_scale = (
            compute_scale_from_samples(s.alt_prev_pts.reshape(-1, 2))
            if s.alt_prev_pts is not None
            else None
        )

        cam_pos = estimate_marker_pose_pnp(
            best_pts4.astype(np.float32), self._k_alt, 1.0
        )
        if cam_pos is not None and np.isfinite(cam_pos).all():
            s.alt_agl_state = float(cam_pos[2])

        x_nav = -s.last_xy[0] if config.flip_x_marker else s.last_xy[0]
        y_nav = -s.last_xy[1] if config.flip_y_marker else s.last_xy[1]
        return (
            float(x_nav * config.marker_size_xy_m),
            float(y_nav * config.marker_size_xy_m),
            float(s.alt_agl_state),
        )

    def step(
        self,
        frame_marker: np.ndarray,
        ts_sec: float,
        frame_id: int | None,
    ) -> TrajectoryPoint:
        s = self._state
        config = self._config
        s.processed += 1
        cur_gray = preprocess_gray(frame_marker)
        cur_roi = make_roi_mask(cur_gray, config.roi_top_ratio)
        alt_gray = cv2.cvtColor(frame_marker, cv2.COLOR_BGR2GRAY)

        self._update_altitude(frame_marker, alt_gray)
        s.alt_prev_gray = alt_gray

        reset = self._try_direct_redetect(frame_marker, ts_sec)

        track = self._lk_track_step(cur_gray, ts_sec, reset)

        if (
            not track.accepted
            or track.force_redetect
            or s.prev_pts_marker is None
            or len(s.prev_pts_marker) < config.min_track_pts_marker
        ):
            s.prev_pts_marker = self._detect_features(cur_gray, cur_roi)
        s.prev_gray_marker = cur_gray
        s.prev_roi_marker = cur_roi

        x_nav = -s.last_xy[0] if config.flip_x_marker else s.last_xy[0]
        y_nav = -s.last_xy[1] if config.flip_y_marker else s.last_xy[1]
        new_pos = np.array(
            [
                float(x_nav * config.marker_size_xy_m),
                float(y_nav * config.marker_size_xy_m),
                float(s.alt_agl_state),
            ],
            dtype=float,
        )
        s.seq += 1
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

    # ── helpers ──────────────────────────────────────────────────

    def _detect_features(
        self, gray: np.ndarray | None, mask: np.ndarray | None
    ) -> np.ndarray | None:
        if gray is None:
            return None
        config = self._config
        return cv2.goodFeaturesToTrack(
            gray,
            maxCorners=config.max_corners_marker,
            qualityLevel=config.gftt_quality,
            minDistance=config.gftt_min_dist,
            blockSize=config.gftt_block,
            mask=mask,
        )

    def _update_altitude(self, frame_marker: np.ndarray, alt_gray: np.ndarray) -> None:
        s = self._state
        config = self._config
        alt_marker = detect_red_square_corners_alt(frame_marker)
        if alt_marker is not None and polygon_area(alt_marker) > config.marker_area_min:
            s.alt_marker_seen = True
            s.alt_marker_cnt += 1
            cam_pos = estimate_marker_pose_pnp(
                alt_marker.astype(np.float32), self._k_alt, 1.0
            )
            if cam_pos is not None and np.isfinite(cam_pos).all():
                s.alt_agl_state = ema_altitude_from_pnp(
                    s.alt_agl_state, float(cam_pos[2]), config
                )
            s.alt_prev_pts = cv2.goodFeaturesToTrack(
                alt_gray,
                config.alt_gftt_max_corners,
                config.alt_gftt_quality,
                config.alt_gftt_min_dist,
            )
            s.alt_prev_scale = (
                compute_scale_from_samples(s.alt_prev_pts.reshape(-1, 2))
                if s.alt_prev_pts is not None
                else None
            )
            return

        if s.alt_marker_seen and s.alt_marker_cnt > config.alt_marker_lost_cnt:
            s.alt_marker_seen = False
        if (
            (not s.alt_marker_seen)
            and s.alt_prev_pts is not None
            and s.alt_prev_gray is not None
        ):
            upd: AltitudeUpdate = estimate_altitude_from_scale(
                prev_gray=s.alt_prev_gray,
                cur_gray=alt_gray,
                prev_pts=s.alt_prev_pts,
                prev_scale=s.alt_prev_scale,
                current_alt=s.alt_agl_state,
                lk_params=self._lk_alt,
                config=config,
            )
            s.alt_agl_state = upd.altitude
            s.alt_prev_pts = upd.next_pts
            s.alt_prev_scale = upd.next_scale
        else:
            s.alt_prev_pts = cv2.goodFeaturesToTrack(
                alt_gray,
                config.alt_gftt_max_corners,
                config.alt_gftt_quality,
                config.alt_gftt_min_dist,
            )

    def _try_direct_redetect(self, frame_marker: np.ndarray, ts_sec: float) -> int:
        s = self._state
        config = self._config
        if config.marker_check_every <= 0:
            return 0
        if s.processed % config.marker_check_every != 0:
            return 0

        pts_m, _ = detect_red_marker_corners(frame_marker)
        if pts_m is None or polygon_area(pts_m) <= config.marker_area_min:
            return 0

        H_direct = cv2.getPerspectiveTransform(
            pts_m.astype(np.float32), _DST_MARKER_NAV_M
        ).astype(np.float64)
        xy_direct = project_point(H_direct, s.track_x, s.track_y)
        if xy_direct is None:
            return 0

        dist = float(np.hypot(xy_direct[0] - s.last_xy[0], xy_direct[1] - s.last_xy[1]))
        if dist >= config.marker_check_dist_px:
            return 0

        s.H_prev_to_plane = H_direct
        s.last_xy = xy_direct
        s.last_accept_t_abs = ts_sec
        return 1

    def _lk_track_step(
        self, cur_gray: np.ndarray, ts_sec: float, reset: int
    ) -> _MarkerTrackOutcome:
        s = self._state
        config = self._config

        if (
            s.prev_pts_marker is None
            or len(s.prev_pts_marker) < config.min_track_pts_marker
        ):
            s.prev_pts_marker = self._detect_features(
                s.prev_gray_marker, s.prev_roi_marker
            )

        if (
            s.prev_pts_marker is None
            or len(s.prev_pts_marker) < config.min_track_pts_marker
            or s.prev_gray_marker is None
        ):
            return _MarkerTrackOutcome(accepted=False)

        result: LKTrackResult = lk_ransac_homography(
            prev_gray=s.prev_gray_marker,
            cur_gray=cur_gray,
            prev_pts=s.prev_pts_marker,
            lk_params=self._lk_params,
            config=config,
            H_guess_prev_to_cur=s.H_guess_prev_to_cur,
        )

        if (
            result.H_prev_to_cur is None
            or result.inlier_mask is None
            or s.H_prev_to_plane is None
        ):
            return _MarkerTrackOutcome(
                accepted=False, force_redetect=result.force_redetect
            )

        inliers_cnt = int(result.inlier_mask.sum())
        n_total = max(1, len(result.p0_good))
        ratio = float(inliers_cnt) / float(n_total)
        if (
            inliers_cnt >= config.min_inliers_marker
            and ratio >= config.min_inlier_ratio_marker
        ):
            s.H_guess_prev_to_cur = result.H_prev_to_cur

        min_inl_dyn = max(config.min_inliers_marker, int(0.30 * n_total))
        if inliers_cnt < min_inl_dyn or ratio < config.min_inlier_ratio_marker:
            return _MarkerTrackOutcome(
                accepted=False, force_redetect=result.force_redetect
            )

        inl_mask = result.inlier_mask.reshape(-1).astype(bool)
        p1_in = result.p1_good[inl_mask]

        H_cur_to_plane, xy_candidate = project_to_ground_plane(
            s.H_prev_to_plane,
            result.H_prev_to_cur,
            p1_in,
            fallback_xy_px=(s.track_x, s.track_y),
        )
        if H_cur_to_plane is None or xy_candidate is None:
            return _MarkerTrackOutcome(
                accepted=False, force_redetect=result.force_redetect
            )

        dt = float(ts_sec - s.last_accept_t_abs)
        if dt <= 0:
            dt = 1.0 / max(float(config.fps), 1e-6)
        step_px = float(
            np.hypot(xy_candidate[0] - s.last_xy[0], xy_candidate[1] - s.last_xy[1])
        )
        max_step = config.max_speed_marker * dt * float(config.max_step_scale_marker)
        if not (reset == 1 or step_px <= max_step):
            return _MarkerTrackOutcome(
                accepted=False, force_redetect=result.force_redetect
            )

        s.last_accept_t_abs = float(ts_sec)
        s.H_prev_to_plane = H_cur_to_plane
        s.last_xy = (float(xy_candidate[0]), float(xy_candidate[1]))
        s.prev_pts_marker = p1_in.reshape(-1, 1, 2).astype(np.float32)
        return _MarkerTrackOutcome(accepted=True, force_redetect=result.force_redetect)

    def set_last_accept_ts(self, ts_sec: float) -> None:
        """Set reference timestamp after marker-mode init replay."""
        self._state.last_accept_t_abs = float(ts_sec)

    def reset_sequence(self) -> None:
        """Reset externally visible sequence after hidden init replay."""
        self._state.seq = 0


# ════════════════════════════════════════════════════════════════════
# NoMarkerEngine
# ════════════════════════════════════════════════════════════════════


@dataclass
class _NoMarkerState:
    """Mutable state of a running no-marker engine."""

    seq: int = 0
    pos: np.ndarray = field(default_factory=lambda: np.zeros(3, dtype=float))
    prev_gray_nm: np.ndarray | None = None
    prev_pts_nm: np.ndarray | None = None
    flow_ema: np.ndarray = field(default_factory=lambda: np.zeros(2, dtype=float))
    traj_points: list[np.ndarray] = field(default_factory=list)


class NoMarkerEngine:
    """Monocular odometry when no marker is present.

    Sparse LK flow on the nav-resolution frame with dense phase-correlation
    fallback; per-frame shift accumulated into ``pos[:2]`` (z stays at 0).
    Applies Laplacian smoothing when per-frame jumps exceed the configured
    thresholds (this matches upstream — smoothing is no-marker-only).
    """

    def __init__(self, mission_id: str, config: NavigationTuning) -> None:
        self._mission_id = mission_id
        self._config = config
        self._lk_nm = _build_no_marker_lk_params()
        self._state = _NoMarkerState()

    def seed_from_init(self, first_frame_bgr: np.ndarray) -> tuple[float, float, float]:
        """Initialise state from the first buffered frame."""
        s = self._state
        config = self._config
        nav_gray = cv2.cvtColor(
            cv2.resize(first_frame_bgr, (config.nav_width, config.nav_height)),
            cv2.COLOR_BGR2GRAY,
        )
        s.prev_gray_nm = nav_gray
        s.prev_pts_nm = cv2.goodFeaturesToTrack(
            nav_gray,
            config.alt_gftt_max_corners,
            config.alt_gftt_quality,
            config.alt_gftt_min_dist,
        )
        s.pos = np.zeros(3, dtype=float)
        s.traj_points = [s.pos.copy()]
        return (0.0, 0.0, 0.0)

    def step(
        self,
        frame_bgr: np.ndarray,
        ts_sec: float,
        frame_id: int | None,
    ) -> TrajectoryPoint:
        s = self._state
        config = self._config

        nav_gray = cv2.cvtColor(
            cv2.resize(frame_bgr, (config.nav_width, config.nav_height)),
            cv2.COLOR_BGR2GRAY,
        )
        shift = optical_flow_lk_or_phase(
            prev_gray=s.prev_gray_nm if s.prev_gray_nm is not None else nav_gray,
            cur_gray=nav_gray,
            prev_pts=s.prev_pts_nm,
            lk_params=self._lk_nm,
            config=config,
        )

        s.flow_ema = (1.0 - config.no_marker_flow_alpha) * s.flow_ema + (
            config.no_marker_flow_alpha * np.array([shift.dx, shift.dy], dtype=float)
        )
        step_vec = np.array([-s.flow_ema[0], -s.flow_ema[1]], dtype=float)
        step_len = float(np.linalg.norm(step_vec))
        if step_len > config.no_marker_max_step_px:
            step_vec *= config.no_marker_max_step_px / step_len

        new_pos = s.pos.copy()
        new_pos[:2] = new_pos[:2] + step_vec

        if s.traj_points:
            prev = s.traj_points[-1]
            jump_xy = float(np.linalg.norm(new_pos[:2] - prev[:2]))
            jump_z = float(abs(new_pos[2] - prev[2]))
            if jump_xy > config.smooth_jump_xy or jump_z > config.smooth_jump_z:
                new_pos = laplacian_smooth_window(s.traj_points, new_pos, config)

        s.pos = new_pos
        s.prev_gray_nm = nav_gray
        s.prev_pts_nm = shift.next_pts
        s.seq += 1
        s.traj_points.append(new_pos.copy())

        return TrajectoryPoint(
            mission_id=self._mission_id,
            seq=s.seq,
            ts_sec=float(ts_sec),
            x=float(new_pos[0]),
            y=float(new_pos[1]),
            z=float(new_pos[2]),
            source=TrajectorySource.OPTICAL_FLOW,
            frame_id=frame_id,
        )

    def reset_sequence(self) -> None:
        """Reset internal sequence after init replay."""
        self._state.seq = 0


# ════════════════════════════════════════════════════════════════════
# NavigationEngine — orchestrator
# ════════════════════════════════════════════════════════════════════


@dataclass
class _Init:
    """Buffered init-phase state (frames pending marker probe)."""

    buffer: list[tuple[int | None, float, np.ndarray]] = field(default_factory=list)


class NavigationEngine:
    """Top-level engine: buffers first frames, picks a nav mode, delegates.

    Implements the ``NavigationEnginePort`` contract: ``reset`` /
    ``step(frame_bgr, ts_sec, frame_id) → TrajectoryPoint | None``.

    The first ``auto_marker_seconds * fps`` frames are buffered. Among
    them we look for red-marker candidates; if at least one survives the
    area filter we commit to ``MarkerEngine`` and replay the emitted
    marker init-pose as ``seq=0``. Otherwise we fall back to
    ``NoMarkerEngine`` starting from the first buffered frame. Mode is
    frozen after init.
    """

    def __init__(
        self,
        mission_id: str,
        config: NavigationTuning | None = None,
    ) -> None:
        self._mission_id = mission_id
        self._config = config or NavigationTuning()
        self._init = _Init()
        self._marker: MarkerEngine | None = None
        self._no_marker: NoMarkerEngine | None = None
        self._initialised = False
        self._seq = 0
        self._emitted_points = 0
        # ``nav_mode`` mirrors diplom-prod's mode pinning. ``NO_MARKER``
        # starts optical-flow odometry immediately; ``MARKER`` keeps the
        # marker init window; ``AUTO`` (or ``None``) probes and picks.
        self._forced_mode: NavMode | None = None

    # ── NavigationEnginePort ────────────────────────────────────

    def reset(
        self,
        *,
        nav_mode: NavMode | None = None,
        fps: float | None = None,
    ) -> None:
        self._init = _Init()
        self._marker = None
        self._no_marker = None
        self._initialised = False
        self._seq = 0
        self._emitted_points = 0
        if fps is not None and fps > 0.0 and abs(fps - self._config.fps) > 1e-9:
            # NavigationTuning is frozen so the engine rebuilds it with
            # the real source FPS — that fps drives auto_marker_seconds
            # and the ``dt`` fallback inside the marker speed gate.
            self._config = replace(self._config, fps=float(fps))
        if nav_mode is None or nav_mode == NavMode.AUTO:
            self._forced_mode = None
        else:
            self._forced_mode = nav_mode
        logger.debug(
            "NavigationEngine.reset: mission_id=%s nav_mode=%s fps=%.3f",
            self._mission_id,
            "auto" if self._forced_mode is None else str(self._forced_mode),
            self._config.fps,
        )

    def step(
        self,
        frame_bgr: object,
        ts_sec: float,
        frame_id: int | None = None,
    ) -> TrajectoryPoint | None:
        frame = np.asarray(frame_bgr)
        if frame.ndim != 3 or frame.shape[2] != 3:
            logger.debug(
                "NavigationEngine.step: skip frame_id=%s ts=%.3f shape=%s",
                frame_id,
                ts_sec,
                frame.shape if hasattr(frame, "shape") else None,
            )
            return None

        if not self._initialised:
            return self._collect_init_frame(frame, ts_sec, frame_id)

        if self._marker is not None:
            frame_marker = cv2.resize(
                frame, (self._config.marker_resize_w, self._config.marker_resize_h)
            )
            point = self._marker.step(frame_marker, ts_sec, frame_id)
            return self._log_emitted_point(
                point,
                frame_id,
                ts_sec,
                frame.shape,
                selected_mode="marker",
                marker_found=True,
            )
        assert self._no_marker is not None
        point = self._no_marker.step(frame, ts_sec, frame_id)
        return self._log_emitted_point(
            point,
            frame_id,
            ts_sec,
            frame.shape,
            selected_mode="no_marker",
            marker_found=False,
        )

    # ── init phase ──────────────────────────────────────────────

    def _collect_init_frame(
        self, frame_bgr: np.ndarray, ts_sec: float, frame_id: int | None
    ) -> TrajectoryPoint | None:
        # When the caller explicitly pins nav_mode=NO_MARKER we skip the
        # marker init buffer: no-marker pose starts at origin from the
        # very first raw frame.
        if self._forced_mode == NavMode.NO_MARKER:
            return self._start_no_marker_immediately(frame_bgr, ts_sec, frame_id)

        config = self._config
        max_init = max(1, int(config.auto_marker_seconds * config.fps))
        self._init.buffer.append((frame_id, ts_sec, frame_bgr.copy()))
        logger.debug(
            "NavigationEngine.step: frame_id=%s ts_sec=%.6f input_shape=%s "
            "selected_mode=init marker_found=%s source=%s emitted_points=%d",
            frame_id,
            ts_sec,
            frame_bgr.shape,
            None,
            None,
            self._emitted_points,
        )
        if len(self._init.buffer) < max_init:
            return None
        return self._finalise_init()

    def _start_no_marker_immediately(
        self, frame_bgr: np.ndarray, ts_sec: float, frame_id: int | None
    ) -> TrajectoryPoint:
        """Initialise no-marker pipeline from the very first frame."""
        engine = NoMarkerEngine(mission_id=self._mission_id, config=self._config)
        x, y, z = engine.seed_from_init(frame_bgr)
        self._no_marker = engine
        self._initialised = True
        self._init = _Init()
        logger.info(
            "Navigation init: mission_id=%s mode=no_marker (forced) "
            "first_frame_id=%s ts=%.3f shape=%s",
            self._mission_id,
            frame_id,
            ts_sec,
            frame_bgr.shape,
        )
        point = TrajectoryPoint(
            mission_id=self._mission_id,
            seq=0,
            ts_sec=float(ts_sec),
            x=float(x),
            y=float(y),
            z=float(z),
            source=TrajectorySource.OPTICAL_FLOW,
            frame_id=frame_id,
        )
        return self._log_emitted_point(
            point,
            frame_id,
            ts_sec,
            frame_bgr.shape,
            selected_mode="no_marker",
            marker_found=False,
        )

    def _finalise_init(self) -> TrajectoryPoint | None:
        config = self._config
        buf = self._init.buffer
        candidates: list[
            tuple[int, float, np.ndarray, int | None, float, np.ndarray]
        ] = []
        for idx, (fid, ts, orig) in enumerate(buf):
            fm = cv2.resize(orig, (config.marker_resize_w, config.marker_resize_h))
            pts, _ = detect_red_marker_corners(fm)
            if pts is None:
                continue
            area = polygon_area(pts)
            area_max = (config.marker_resize_w * config.marker_resize_h) * (
                config.marker_area_max_ratio_init
            )
            if config.marker_area_min_init < area < area_max:
                candidates.append((idx, area, pts, fid, ts, fm))

        if candidates:
            return self._init_marker_mode(candidates)
        if self._forced_mode == NavMode.MARKER:
            logger.warning(
                "Navigation init: mission_id=%s nav_mode=marker forced but "
                "no marker found in init window — falling back to no_marker",
                self._mission_id,
            )
        return self._init_no_marker_mode()

    def _init_marker_mode(
        self,
        candidates: list[tuple[int, float, np.ndarray, int | None, float, np.ndarray]],
    ) -> TrajectoryPoint:
        config = self._config
        candidates.sort(key=lambda x: x[1], reverse=True)
        topk = candidates[: min(config.marker_init_topk, len(candidates))]
        stack = np.stack([c[2] for c in topk], axis=0)
        median_corners = np.median(stack, axis=0)
        best = min(topk, key=lambda c: float(np.linalg.norm(c[2] - median_corners)))
        best_idx, _, best_pts4, best_fid, best_ts, best_fm = best
        init_frames_count = len(self._init.buffer)
        best_raw_shape = self._init.buffer[best_idx][2].shape

        engine = MarkerEngine(mission_id=self._mission_id, config=config)
        x, y, z = engine.seed_from_init(best_pts4.astype(np.float32), best_fm)
        engine.set_last_accept_ts(float(best_ts))

        self._marker = engine
        self._initialised = True
        self._seq = 0

        engine.step(best_fm, ts_sec=float(best_ts), frame_id=best_fid)
        for fid, ts, orig in self._init.buffer[best_idx + 1 :]:
            fm = cv2.resize(orig, (config.marker_resize_w, config.marker_resize_h))
            engine.step(fm, ts_sec=float(ts), frame_id=fid)
        engine.reset_sequence()
        self._init = _Init()

        logger.info(
            "Navigation init: mission_id=%s mode=marker init_frames=%d "
            "candidates=%d best_frame_id=%s ts=%.3f xyz=(%.3f,%.3f,%.3f)",
            self._mission_id,
            init_frames_count,
            len(candidates),
            best_fid,
            best_ts,
            x,
            y,
            z,
        )

        point = TrajectoryPoint(
            mission_id=self._mission_id,
            seq=0,
            ts_sec=float(best_ts),
            x=float(x),
            y=float(y),
            z=float(z),
            source=TrajectorySource.MARKER,
            frame_id=best_fid,
        )
        return self._log_emitted_point(
            point,
            best_fid,
            best_ts,
            best_raw_shape,
            selected_mode="marker",
            marker_found=True,
        )

    def _init_no_marker_mode(self) -> TrajectoryPoint:
        buf = self._init.buffer
        first_fid, first_ts, first_frame = buf[0]

        engine = NoMarkerEngine(mission_id=self._mission_id, config=self._config)
        x, y, z = engine.seed_from_init(first_frame)

        self._no_marker = engine
        self._initialised = True
        self._seq = 0

        logger.info(
            "Navigation init: mission_id=%s mode=no_marker init_frames=%d "
            "first_frame_id=%s ts=%.3f xyz=(%.3f,%.3f,%.3f)",
            self._mission_id,
            len(buf),
            first_fid,
            first_ts,
            x,
            y,
            z,
        )

        remaining = buf[1:]
        self._init = _Init()

        first_point = TrajectoryPoint(
            mission_id=self._mission_id,
            seq=0,
            ts_sec=float(first_ts),
            x=float(x),
            y=float(y),
            z=float(z),
            source=TrajectorySource.OPTICAL_FLOW,
            frame_id=first_fid,
        )
        # Replay buffered frames into the fresh engine so feature/flow
        # state matches what upstream would have after running through
        # the init window. Emitted points are discarded; seq is reset so
        # the next external step gets seq=1.
        engine.step(first_frame, ts_sec=float(first_ts), frame_id=first_fid)
        for fid, ts, frame in remaining:
            engine.step(frame, ts_sec=float(ts), frame_id=fid)
        engine.reset_sequence()
        return self._log_emitted_point(
            first_point,
            first_fid,
            first_ts,
            first_frame.shape,
            selected_mode="no_marker",
            marker_found=False,
        )

    def _log_emitted_point(
        self,
        point: TrajectoryPoint,
        frame_id: int | None,
        ts_sec: float,
        input_shape: tuple[int, ...],
        *,
        selected_mode: str,
        marker_found: bool,
    ) -> TrajectoryPoint:
        self._emitted_points += 1
        logger.debug(
            "NavigationEngine.step: frame_id=%s ts_sec=%.6f input_shape=%s "
            "selected_mode=%s marker_found=%s source=%s "
            "x=%.6f y=%.6f z=%.6f emitted_points=%d",
            frame_id,
            ts_sec,
            input_shape,
            selected_mode,
            marker_found,
            str(point.source),
            point.x,
            point.y,
            point.z,
            self._emitted_points,
        )
        return point


# ── Convenience ─────────────────────────────────────────────────────


def new_engine(
    mission_id: str | None = None, config: NavigationTuning | None = None
) -> NavigationEngine:
    """Convenience constructor with a default mission id."""
    return NavigationEngine(
        mission_id=mission_id or f"auto-{uuid4().hex[:8]}",
        config=config,
    )
