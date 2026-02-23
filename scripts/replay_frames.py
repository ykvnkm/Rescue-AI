from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib import request


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames-dir", required=True, help="Path to folder with PNG/JPG frames")
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--fps", type=float, default=2.0)
    parser.add_argument("--high-score", type=float, default=0.95)
    parser.add_argument("--low-score", type=float, default=0.2)
    args = parser.parse_args()

    frames_dir = Path(args.frames_dir)
    if not frames_dir.exists():
        raise SystemExit(f"frames dir not found: {frames_dir}")

    frame_files = sorted(
        [p for p in frames_dir.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg"}]
    )
    if not frame_files:
        raise SystemExit("no frames found")

    mission = post_json(f"{args.api_base}/v1/missions", {})
    mission_id = mission["mission_id"]
    print(f"[INFO] mission_id={mission_id}, frames={len(frame_files)}")

    dt = 1.0 / args.fps if args.fps > 0 else 0.5

    for idx, frame_path in enumerate(frame_files):
        label_path = frame_path.with_suffix(".txt")
        has_label = label_path.exists() and label_path.read_text(encoding="utf-8").strip() != ""
        score = args.high_score if has_label else args.low_score

        payload = {
            "mission_id": mission_id,
            "frame_id": idx,
            "ts_sec": round(idx * dt, 3),
            "score": score,
        }
        resp = post_json(f"{args.api_base}/v1/frames", payload)
        print(f"[FRAME {idx}] {frame_path.name} -> alert_created={resp['alert_created']}")
        time.sleep(dt)

    print(f"[DONE] mission_id={mission_id}")
    print(f"Check alerts: {args.api_base}/v1/alerts?mission_id={mission_id}")


if __name__ == "__main__":
    main()
