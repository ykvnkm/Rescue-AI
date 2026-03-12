from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from config import config
from libs.core.application.models import AlertRuleConfig
from services.detection_service.infrastructure.runtime_contract import (
    load_stream_contract,
)

DEFAULT_CONTRACT_PATH = Path("configs/nsu_frames_yolov8n_alert_contract.yaml")
DEFAULT_MODEL_URL = (
    "https://github.com/ykvnkm/rescueai-models/releases/download/v1/"
    "yolov8n_baseline_multiscale.pt"
)


def load_alert_rules_and_metadata() -> tuple[AlertRuleConfig, dict[str, object]]:
    payload = yaml.safe_load(DEFAULT_CONTRACT_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Invalid alert contract payload")

    contract = load_stream_contract()
    score_threshold = float(contract.alert_rules.score_threshold)

    alert_rules = AlertRuleConfig(
        score_threshold=score_threshold,
        window_sec=float(contract.alert_rules.window_sec),
        quorum_k=int(contract.alert_rules.quorum_k),
        cooldown_sec=float(contract.alert_rules.cooldown_sec),
        gap_end_sec=float(contract.alert_rules.gap_end_sec),
        gt_gap_end_sec=float(contract.alert_rules.gt_gap_end_sec),
        match_tolerance_sec=float(contract.alert_rules.match_tolerance_sec),
    )

    report_metadata: dict[str, object] = {
        "config_name": str(payload.get("name", "unknown_contract")),
        "config_hash": hashlib.sha256(DEFAULT_CONTRACT_PATH.read_bytes()).hexdigest(),
        "config_path": str(DEFAULT_CONTRACT_PATH),
        "model_url": str(payload.get("model_url", DEFAULT_MODEL_URL)),
        "service_version": config.service_version(),
    }
    return alert_rules, report_metadata
