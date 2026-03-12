from __future__ import annotations

import hashlib
import os
from pathlib import Path

import yaml

from libs.core.application.models import AlertRuleConfig

# pylint: disable=duplicate-code

DEFAULT_CONTRACT_PATH = Path("configs/nsu_frames_yolov8n_alert_contract.yaml")
DEFAULT_MODEL_URL = (
    "https://github.com/ykvnkm/rescueai-models/releases/download/v1/"
    "yolov8n_baseline_multiscale.pt"
)


def load_alert_rules_and_metadata() -> tuple[AlertRuleConfig, dict[str, object]]:
    payload = yaml.safe_load(DEFAULT_CONTRACT_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Invalid alert contract payload")

    alert = payload.get("alert", {})
    eval_cfg = payload.get("eval", {})

    thresholds = eval_cfg.get("thresholds", [0.2])
    score_threshold = float(thresholds[0] if thresholds else 0.2)

    alert_rules = AlertRuleConfig(
        score_threshold=score_threshold,
        window_sec=float(alert.get("window_sec", 1.0)),
        quorum_k=int(alert.get("quorum_k", 1)),
        cooldown_sec=float(alert.get("cooldown_sec", 1.5)),
        gap_end_sec=float(alert.get("gap_end_sec", 1.2)),
        gt_gap_end_sec=float(alert.get("gt_gap_end_sec", 1.0)),
        match_tolerance_sec=float(alert.get("match_tolerance_sec", 1.2)),
    )

    report_metadata: dict[str, object] = {
        "config_name": str(payload.get("name", "unknown_contract")),
        "config_hash": hashlib.sha256(DEFAULT_CONTRACT_PATH.read_bytes()).hexdigest(),
        "config_path": str(DEFAULT_CONTRACT_PATH),
        "model_url": str(payload.get("model_url", DEFAULT_MODEL_URL)),
        "service_version": os.getenv("SERVICE_VERSION", "dev"),
    }
    return alert_rules, report_metadata
