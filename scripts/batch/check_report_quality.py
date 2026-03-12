from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate batch report quality gates")
    parser.add_argument("--report", required=True, help="Path to report.json")
    parser.add_argument("--min-recall", type=float, default=0.7)
    parser.add_argument("--max-fp-per-minute", type=float, default=5.0)
    parser.add_argument("--max-ttfc-sec", type=float, default=6.5)
    return parser.parse_args()


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def validate(
    payload: dict[str, object],
    min_recall: float,
    max_fp_per_minute: float,
    max_ttfc_sec: float,
) -> list[str]:
    errors: list[str] = []

    status = str(payload.get("status", "unknown"))
    if status not in {"completed", "partial"}:
        errors.append(f"invalid_status:{status}")

    recall = _as_float(payload.get("recall_event"))
    fp_per_minute = _as_float(payload.get("fp_per_minute"))
    ttfc_sec = _as_float(payload.get("ttfc_sec"))

    kpi_validity = payload.get("kpi_validity")
    is_not_applicable = False
    if isinstance(kpi_validity, dict):
        recall_state = str(kpi_validity.get("recall_event", ""))
        is_not_applicable = recall_state == "not_applicable"

    if not is_not_applicable:
        if recall is None:
            errors.append("recall_event_missing")
        elif recall < min_recall:
            errors.append(f"recall_event_too_low:{recall}")

        if fp_per_minute is None:
            errors.append("fp_per_minute_missing")
        elif fp_per_minute > max_fp_per_minute:
            errors.append(f"fp_per_minute_too_high:{fp_per_minute}")

        if ttfc_sec is None:
            errors.append("ttfc_sec_missing")
        elif ttfc_sec > max_ttfc_sec:
            errors.append(f"ttfc_sec_too_high:{ttfc_sec}")

    return errors


def main() -> None:
    args = parse_args()
    payload = json.loads(Path(args.report).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("report.json payload must be object")

    errors = validate(
        payload=payload,
        min_recall=args.min_recall,
        max_fp_per_minute=args.max_fp_per_minute,
        max_ttfc_sec=args.max_ttfc_sec,
    )
    if errors:
        raise SystemExit("quality_gates_failed:" + ",".join(errors))
    print("quality_gates_passed")


if __name__ == "__main__":
    main()
