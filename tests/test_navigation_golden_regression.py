"""Env-gated regression: MarkerEngine vs golden trajectory CSV from diplom-prod.

Skipped unless ``RESCUE_AI_GOLDEN_NAV=1``. Reads two local files (paths
overridable via env), runs the engine end-to-end on the marker video, and
compares the resulting trajectory to the golden CSV with a loose tolerance.

Defaults:
    video: /Users/ykvnkm/Desktop/Diplom/test_videos/video_drone.mp4
    csv:   /Users/ykvnkm/Downloads/20260415_125514_CPU_c75a3...
            .../trajectory.csv (see DEFAULT_CSV constant for full path)

Run locally with:
    RESCUE_AI_GOLDEN_NAV=1 uv run pytest tests/test_navigation_golden_regression.py -s
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

import numpy as np
import pytest

from rescue_ai.domain.value_objects import NavMode
from rescue_ai.navigation.engine import NavigationEngine
from rescue_ai.navigation.tuning import NavigationTuning

DEFAULT_VIDEO = Path(
    "/Users/ykvnkm/Desktop/Diplom/test_videos/video_drone.mp4",
)
DEFAULT_CSV = Path(
    "/Users/ykvnkm/Downloads/"
    "20260415_125514_CPU_c75a351a9264478194fde3b6eee30c3a/trajectory.csv",
)


def _norm_key(name: str) -> str:
    return name.strip().lower().replace("_", "").replace(" ", "")


def _parse_float(value: str) -> float:
    return float(value.strip().replace(",", "."))


def _pick_float(row: dict[str, str], aliases: tuple[str, ...]) -> float:
    norm_row = {
        _norm_key(k): v for k, v in row.items() if k is not None and v is not None
    }
    for alias in aliases:
        value = norm_row.get(_norm_key(alias))
        if value is not None and value.strip() != "":
            return _parse_float(value)
    raise KeyError(
        f"missing columns {aliases}; available columns: {sorted(norm_row.keys())}"
    )


@pytest.mark.skipif(
    os.environ.get("RESCUE_AI_GOLDEN_NAV") != "1",
    reason="set RESCUE_AI_GOLDEN_NAV=1 to run the navigation regression",
)
def test_marker_engine_matches_golden_trajectory() -> None:
    cv2 = pytest.importorskip("cv2")

    video_path = Path(os.environ.get("RESCUE_AI_GOLDEN_VIDEO", str(DEFAULT_VIDEO)))
    csv_path = Path(os.environ.get("RESCUE_AI_GOLDEN_CSV", str(DEFAULT_CSV)))

    if not video_path.exists():
        pytest.skip(f"golden video not found: {video_path}")
    if not csv_path.exists():
        pytest.skip(f"golden csv not found: {csv_path}")

    cap = cv2.VideoCapture(str(video_path))
    assert cap.isOpened(), f"cannot open video {video_path}"
    fps = float(cap.get(cv2.CAP_PROP_FPS)) or 30.0

    engine = NavigationEngine(
        mission_id="golden-regression",
        config=NavigationTuning(fps=fps),
    )
    # Pin the engine to MARKER and feed the real source FPS — that's
    # the contract diplom-prod runs against.
    engine.reset(nav_mode=NavMode.MARKER, fps=fps)

    points: list[tuple[float, float, float, float]] = []
    frame_idx = 0
    while True:
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        ts = frame_idx / max(fps, 1e-6)
        out = engine.step(frame, ts_sec=ts, frame_id=frame_idx)
        if out is not None:
            points.append((out.ts_sec, out.x, out.y, out.z))
        frame_idx += 1
    cap.release()

    assert points, "engine produced no trajectory points"

    golden: list[tuple[float, float, float, float]] = []
    with csv_path.open(encoding="utf-8", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        for row in reader:
            golden.append(
                (
                    _pick_float(row, ("t", "time", "ts", "timestamp")),
                    _pick_float(row, ("x", "x_m", "pos_x", "px")),
                    _pick_float(row, ("y", "y_m", "pos_y", "py")),
                    _pick_float(row, ("z", "z_m", "pos_z", "pz")),
                )
            )
    assert golden, "golden csv is empty"

    # Compare relative drift (end-start) to avoid dependency on absolute
    # coordinate origin differences between pipelines.
    engine_delta = np.array(points[-1][1:]) - np.array(points[0][1:])
    golden_delta = np.array(golden[-1][1:]) - np.array(golden[0][1:])
    diff = engine_delta - golden_delta
    assert abs(diff[0]) < 0.5, f"x drift: {diff[0]:.3f} m"
    assert abs(diff[1]) < 0.5, f"y drift: {diff[1]:.3f} m"
    assert abs(diff[2]) < 0.3, f"z drift: {diff[2]:.3f} m"
