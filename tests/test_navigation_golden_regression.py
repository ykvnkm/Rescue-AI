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

from rescue_ai.navigation.engine import MarkerEngine, MarkerEngineConfig

DEFAULT_VIDEO = Path(
    "/Users/ykvnkm/Desktop/Diplom/test_videos/video_drone.mp4",
)
DEFAULT_CSV = Path(
    "/Users/ykvnkm/Downloads/"
    "20260415_125514_CPU_c75a351a9264478194fde3b6eee30c3a/trajectory.csv",
)


@pytest.mark.skipif(
    os.environ.get("RESCUE_AI_GOLDEN_NAV") != "1",
    reason="set RESCUE_AI_GOLDEN_NAV=1 to run the navigation regression",
)
def test_marker_engine_matches_golden_trajectory() -> (
    None
):  # pylint: disable=too-many-locals
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

    engine = MarkerEngine(
        mission_id="golden-regression",
        config=MarkerEngineConfig(fps=fps),
    )
    engine.reset()

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
    with csv_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            golden.append(
                (
                    float(row.get("t", row.get("time", 0.0))),
                    float(row["x"]),
                    float(row["y"]),
                    float(row["z"]),
                )
            )
    assert golden, "golden csv is empty"

    # Loose tolerance: within 0.5 m on xy at the final point, 0.3 m on z.
    final_engine = np.array(points[-1][1:])
    final_golden = np.array(golden[-1][1:])
    diff = final_engine - final_golden
    assert abs(diff[0]) < 0.5, f"x drift: {diff[0]:.3f} m"
    assert abs(diff[1]) < 0.5, f"y drift: {diff[1]:.3f} m"
    assert abs(diff[2]) < 0.3, f"z drift: {diff[2]:.3f} m"
