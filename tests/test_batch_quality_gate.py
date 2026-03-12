from __future__ import annotations

from scripts.batch.check_report_quality import validate


def test_quality_gate_passes_for_valid_metrics() -> None:
    payload: dict[str, object] = {
        "status": "completed",
        "recall_event": 0.9,
        "fp_per_minute": 1.2,
        "ttfc_sec": 5.0,
        "kpi_validity": {
            "recall_event": "valid",
        },
    }
    errors = validate(
        payload=payload,
        min_recall=0.7,
        max_fp_per_minute=5.0,
        max_ttfc_sec=6.5,
    )
    assert not errors


def test_quality_gate_skips_not_applicable_kpis() -> None:
    payload: dict[str, object] = {
        "status": "completed",
        "kpi_validity": {
            "recall_event": "not_applicable",
        },
    }
    errors = validate(
        payload=payload,
        min_recall=0.7,
        max_fp_per_minute=5.0,
        max_ttfc_sec=6.5,
    )
    assert not errors


def test_quality_gate_fails_for_bad_metrics() -> None:
    payload: dict[str, object] = {
        "status": "completed",
        "recall_event": 0.1,
        "fp_per_minute": 10.0,
        "ttfc_sec": 12.0,
        "kpi_validity": {
            "recall_event": "valid",
        },
    }
    errors = validate(
        payload=payload,
        min_recall=0.7,
        max_fp_per_minute=5.0,
        max_ttfc_sec=6.5,
    )
    assert any(item.startswith("recall_event_too_low") for item in errors)
    assert any(item.startswith("fp_per_minute_too_high") for item in errors)
    assert any(item.startswith("ttfc_sec_too_high") for item in errors)
