"""Loads alert/inference configuration from YAML contract files."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

from rescue_ai.application.inference_config import InferenceConfig
from rescue_ai.domain.ports import ReportMetadataPayload
from rescue_ai.domain.value_objects import AlertRuleConfig

DEFAULT_CONTRACT_PATH = Path("configs/nsu_frames_yolov8n_alert_contract.yaml")
DEFAULT_MODEL_URL = (
    "https://storage.yandexcloud.net/"
    "rescue-ai-models-public/models/"
    "yolov8n_baseline_multiscale/v1/"
    "yolov8n_baseline_multiscale.pt"
)


def _require_mapping(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("Invalid alert contract payload")
    return payload


def _resolve_confidence_threshold(payload: dict[str, object]) -> float:
    eval_cfg = payload.get("eval", {})
    thresholds = (
        eval_cfg.get("thresholds", [0.2]) if isinstance(eval_cfg, dict) else [0.2]
    )
    return float(thresholds[0] if thresholds else 0.2)


def _build_alert_rules(
    payload: dict[str, object],
    confidence_threshold: float,
) -> AlertRuleConfig:
    alert = payload.get("alert", {})
    if not isinstance(alert, dict):
        alert = {}
    return AlertRuleConfig(
        score_threshold=confidence_threshold,
        window_sec=float(alert.get("window_sec", 1.0)),
        quorum_k=int(alert.get("quorum_k", 1)),
        cooldown_sec=float(alert.get("cooldown_sec", 1.5)),
        gap_end_sec=float(alert.get("gap_end_sec", 1.2)),
        gt_gap_end_sec=float(alert.get("gt_gap_end_sec", 1.0)),
        match_tolerance_sec=float(alert.get("match_tolerance_sec", 1.2)),
    )


def _resolve_min_detections_per_frame(payload: dict[str, object]) -> int:
    alert = payload.get("alert", {})
    if not isinstance(alert, dict):
        return 1
    return int(alert.get("min_detections_per_frame", 1))


_SUPPORTED_DETECTORS = {"yolo", "nanodet"}


def _normalize_sha256(value: object) -> str | None:
    return str(value).strip().lower() if value else None


def _build_inference_config(
    payload: dict[str, object],
    confidence_threshold: float,
) -> InferenceConfig:
    infer = payload.get("infer", {})
    if not isinstance(infer, dict):
        infer = {}

    detector_cfg = payload.get("detector", {})
    if not isinstance(detector_cfg, dict):
        detector_cfg = {}
    detector_name = str(detector_cfg.get("name", "yolo")).strip().lower()
    if detector_name not in _SUPPORTED_DETECTORS:
        raise ValueError(
            f"Unsupported detector: {detector_name!r}; "
            f"expected one of {sorted(_SUPPORTED_DETECTORS)}"
        )

    nanodet_cfg = detector_cfg.get("nanodet", {})
    if not isinstance(nanodet_cfg, dict):
        nanodet_cfg = {}

    return InferenceConfig(
        model_url=str(payload.get("model_url", DEFAULT_MODEL_URL)),
        device=str(payload.get("device", "cpu")),
        imgsz=int(infer.get("imgsz", 960)),
        nms_iou=float(infer.get("nms_iou", 0.75)),
        max_det=int(infer.get("max_det", 1000)),
        confidence_threshold=confidence_threshold,
        model_sha256=_normalize_sha256(payload.get("model_sha256")),
        detector_name=detector_name,
        nanodet_config_url=(
            str(nanodet_cfg["config_url"]) if nanodet_cfg.get("config_url") else None
        ),
        nanodet_config_sha256=_normalize_sha256(nanodet_cfg.get("config_sha256")),
        nanodet_onnx_url=(
            str(nanodet_cfg["onnx_url"]) if nanodet_cfg.get("onnx_url") else None
        ),
        nanodet_onnx_sha256=_normalize_sha256(nanodet_cfg.get("onnx_sha256")),
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


def load_stream_contract(
    service_version: str = "dev",
    contract_path: Path | None = None,
) -> StreamContract:
    """Load and resolve the stream contract from ``contract_path`` (or default)."""
    contract_path = contract_path or DEFAULT_CONTRACT_PATH
    payload = _require_mapping(
        yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    )
    config_hash = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    confidence_threshold = _resolve_confidence_threshold(payload)
    rules = _build_alert_rules(payload, confidence_threshold)
    inference = _build_inference_config(payload, confidence_threshold)
    dataset = payload.get("dataset", {})
    if not isinstance(dataset, dict):
        dataset = {}

    return StreamContract(
        dataset_fps=float(dataset.get("fps", 6.0)),
        alert_rules=rules,
        inference=inference,
        min_detections_per_frame=_resolve_min_detections_per_frame(payload),
        config_name=str(payload.get("name", "unknown_contract")),
        config_hash=config_hash,
        config_path=contract_path.as_posix(),
        service_version=service_version,
    )


def load_alert_rules_and_metadata(
    service_version: str = "dev",
) -> tuple[AlertRuleConfig, ReportMetadataPayload]:
    """Return alert rules and report metadata from the stream contract."""
    contract = load_stream_contract(service_version=service_version)

    report_metadata: ReportMetadataPayload = {
        "config_name": contract.config_name,
        "config_hash": contract.config_hash,
        "config_path": contract.config_path,
        "model_url": contract.inference.model_url,
        "service_version": contract.service_version,
    }
    if contract.inference.model_sha256:
        report_metadata["model_sha256"] = contract.inference.model_sha256
    return contract.alert_rules, report_metadata
