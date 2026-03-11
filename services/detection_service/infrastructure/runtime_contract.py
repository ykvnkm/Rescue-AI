from __future__ import annotations

import hashlib
import os
from pathlib import Path

import yaml

from services.detection_service.domain.models import (
    AlertRulesConfig,
    InferenceConfig,
    ReportProvenance,
    StreamContract,
)

DEFAULT_CONTRACT_PATH = Path("configs/nsu_frames_yolov8n_alert_contract.yaml")
DEFAULT_MODEL_KEY = (
    "models/yolov8n_baseline_multiscale/v1/yolov8n_baseline_multiscale.pt"
)


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
    service_version = os.getenv("SERVICE_VERSION", "dev")

    thresholds = eval_cfg.get("thresholds", [0.2])
    confidence_threshold = float(thresholds[0] if thresholds else 0.2)

    model_key = str(payload.get("model_key", DEFAULT_MODEL_KEY))
    device = str(payload.get("device", "cpu"))

    rules = AlertRulesConfig(
        score_threshold=confidence_threshold,
        window_sec=float(alert.get("window_sec", 1.0)),
        quorum_k=int(alert.get("quorum_k", 1)),
        cooldown_sec=float(alert.get("cooldown_sec", 1.5)),
        gap_end_sec=float(alert.get("gap_end_sec", 1.2)),
        gt_gap_end_sec=float(alert.get("gt_gap_end_sec", 1.0)),
        match_tolerance_sec=float(alert.get("match_tolerance_sec", 1.2)),
    )

    inference = InferenceConfig(
        model_path=model_key,
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
            config_path=contract_path.as_posix(),
            service_version=service_version,
        ),
    )