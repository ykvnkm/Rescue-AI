"""Runtime configuration loader for model inference and alert contract."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from libs.core.application.pilot_service import AlertRuleConfig

DEFAULT_CONTRACT_PATH = Path("configs/nsu_frames_yolov8n_alert_contract.yaml")
DEFAULT_MODEL_URL = (
    "https://github.com/ykvnkm/rescueai-models/releases/download/v1/"
    "yolov8n_baseline_multiscale.pt"
)


@dataclass(frozen=True)
class InferenceConfig:
    """Inference parameters for YOLO runtime."""

    model_url: str
    device: str
    imgsz: int
    nms_iou: float
    max_det: int
    confidence_threshold: float


@dataclass(frozen=True)
class ReportProvenance:
    """Reproducibility metadata injected into mission reports."""

    config_name: str
    config_hash: str
    config_path: str
    service_version: str


@dataclass(frozen=True)
class StreamContract:
    """Combined runtime contract used by stream runner and pilot service."""

    dataset_fps: float
    alert_rules: AlertRuleConfig
    inference: InferenceConfig
    min_detections_per_frame: int
    report_provenance: ReportProvenance


def load_stream_contract() -> StreamContract:
    """Load runtime contract from YAML."""
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
    service_version = os.getenv("SERVICE_VERSION", "dev")

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
        report_provenance=ReportProvenance(
            config_name=config_name,
            config_hash=config_hash,
            config_path=str(contract_path),
            service_version=service_version,
        ),
    )
