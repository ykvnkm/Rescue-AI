"""Loads alert/inference configuration from YAML contract files."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

from rescue_ai.config import get_settings
from rescue_ai.domain.entities import AlertRuleConfig, InferenceConfig

DEFAULT_CONTRACT_PATH = Path("configs/nsu_frames_yolov8n_alert_contract.yaml")
DEFAULT_MODEL_URL = (
    "https://storage.yandexcloud.net/"
    "rescue-ai-models-public/models/"
    "yolov8n_baseline_multiscale/v1/"
    "yolov8n_baseline_multiscale.pt"
)


@dataclass(frozen=True)
class StreamContract:
    """Resolved runtime contract (infrastructure-level, not a domain entity)."""

    dataset_fps: float
    alert_rules: AlertRuleConfig
    inference: InferenceConfig
    min_detections_per_frame: int
    config_name: str
    config_hash: str
    config_path: str
    service_version: str


def load_stream_contract() -> StreamContract:
    contract_path = DEFAULT_CONTRACT_PATH
    payload = yaml.safe_load(contract_path.read_text(encoding="utf-8"))

    if not isinstance(payload, dict):
        raise ValueError("Invalid alert contract payload")

    alert = payload.get("alert", {})
    infer = payload.get("infer", {})
    eval_cfg = payload.get("eval", {})
    dataset = payload.get("dataset", {})

    config_name = str(payload.get("name", "unknown_contract"))
    config_hash = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    service_version = get_settings().app.service_version

    thresholds = eval_cfg.get("thresholds", [0.2])
    confidence_threshold = float(thresholds[0] if thresholds else 0.2)

    model_url = str(payload.get("model_url", DEFAULT_MODEL_URL))
    device = str(payload.get("device", "cpu"))

    rules = AlertRuleConfig(
        score_threshold=confidence_threshold,
        window_sec=float(alert.get("window_sec", 1.0)),
        quorum_k=int(alert.get("quorum_k", 1)),
        cooldown_sec=float(alert.get("cooldown_sec", 1.5)),
        gap_end_sec=float(alert.get("gap_end_sec", 1.2)),
        gt_gap_end_sec=float(alert.get("gt_gap_end_sec", 1.0)),
        match_tolerance_sec=float(alert.get("match_tolerance_sec", 1.2)),
    )

    inference = InferenceConfig(
        model_url=model_url,
        device=device,
        imgsz=int(infer.get("imgsz", 960)),
        nms_iou=float(infer.get("nms_iou", 0.75)),
        max_det=int(infer.get("max_det", 1000)),
        confidence_threshold=confidence_threshold,
    )

    return StreamContract(
        dataset_fps=float(dataset.get("fps", 6.0)),
        alert_rules=rules,
        inference=inference,
        min_detections_per_frame=int(alert.get("min_detections_per_frame", 1)),
        config_name=config_name,
        config_hash=config_hash,
        config_path=contract_path.as_posix(),
        service_version=service_version,
    )


def load_alert_rules_and_metadata() -> tuple[AlertRuleConfig, dict[str, object]]:
    contract = load_stream_contract()

    report_metadata: dict[str, object] = {
        "config_name": contract.config_name,
        "config_hash": contract.config_hash,
        "config_path": contract.config_path,
        "model_url": contract.inference.model_url,
        "service_version": contract.service_version,
    }
    return contract.alert_rules, report_metadata
