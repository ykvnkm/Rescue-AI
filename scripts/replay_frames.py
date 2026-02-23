from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path
from urllib import request


@dataclass
class ReplayContext:
    """Runtime context for frame replay requests."""

    api_base: str
    mission_id: str
    high_score: float
    low_score: float


def post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url=url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def resolve_label_path(frame_path: Path, labels_dir: Path | None) -> Path:
    if labels_dir is None:
        return frame_path.with_suffix(".txt")
    return labels_dir / f"{frame_path.stem}.txt"


def has_ground_truth(label_path: Path) -> bool:
    if not label_path.exists():
        return False
    return label_path.read_text(encoding="utf-8").strip() != ""


def replay_frame(
    context: ReplayContext,
    frame_id: int,
    frame_path: Path,
    ts_sec: float,
    has_label: bool,
) -> None:
    score = context.high_score if has_label else context.low_score
    payload = {
        "mission_id": context.mission_id,
        "frame_id": frame_id,
        "ts_sec": ts_sec,
        "score": score,
        "gt_person_present": has_label,
    }
    response = post_json(f"{context.api_base}/v1/frames", payload)
    print(
        f"[FRAME {frame_id}] {frame_path.name} gt={has_label} "
        f"-> alert_created={response['alert_created']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--frames-dir",
        required=True,
        help="Path to folder with PNG/JPG frames",
    )
    parser.add_argument(
        "--labels-dir",
        default="",
        help="Optional path to folder with YOLO txt labels",
    )
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--fps", type=float, default=2.0)
    parser.add_argument("--high-score", type=float, default=0.95)
    parser.add_argument("--low-score", type=float, default=0.05)
    args = parser.parse_args()

    frames_dir = Path(args.frames_dir)
    if not frames_dir.exists():
        raise SystemExit(f"frames dir not found: {frames_dir}")

    labels_dir = Path(args.labels_dir) if args.labels_dir else None
    if labels_dir is not None and not labels_dir.exists():
        raise SystemExit(f"labels dir not found: {labels_dir}")

    frame_files = sorted(
        [
            path
            for path in frames_dir.iterdir()
            if path.suffix.lower() in {".png", ".jpg", ".jpeg"}
        ]
    )
    if not frame_files:
        raise SystemExit("no frames found")

    mission = post_json(f"{args.api_base}/v1/missions", {})
    context = ReplayContext(
        api_base=args.api_base,
        mission_id=mission["mission_id"],
        high_score=args.high_score,
        low_score=args.low_score,
    )
    print(f"[INFO] mission_id={context.mission_id}, " f"frames={len(frame_files)}")

    dt = 1.0 / args.fps if args.fps > 0 else 0.5

    for idx, frame_path in enumerate(frame_files):
        label_path = resolve_label_path(
            frame_path=frame_path,
            labels_dir=labels_dir,
        )
        gt_present = has_ground_truth(label_path)
        replay_frame(
            context=context,
            frame_id=idx,
            frame_path=frame_path,
            ts_sec=round(idx * dt, 3),
            has_label=gt_present,
        )
        time.sleep(dt)

    print(f"[DONE] mission_id={context.mission_id}")
    print(
        "Check alerts: " f"{context.api_base}/v1/alerts?mission_id={context.mission_id}"
    )
    print(
        "Check episodes: "
        f"{context.api_base}/v1/missions/{context.mission_id}/episodes"
    )


if __name__ == "__main__":
    main()
