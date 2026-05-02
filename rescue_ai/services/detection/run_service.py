"""Entry point for rescue-ai-detection pod.

    python -m rescue_ai.services.detection.run_service

Использует ту же ``InferenceConfig``, что и монолитный API
(``rescue_ai.application.inference_config.load_inference_config``),
поэтому модель/imgsz/conf — те же без задвоений.
"""

from __future__ import annotations

import logging
import os

import uvicorn

from rescue_ai.config import get_settings
from rescue_ai.domain.ports import DetectorPort
from rescue_ai.infrastructure.contract_loader import load_stream_contract
from rescue_ai.infrastructure.detectors import build_detector
from rescue_ai.services.detection.app import build_app


def _factory() -> DetectorPort:
    settings = get_settings()
    contract = load_stream_contract(service_version=settings.app.service_version)
    return build_detector(contract.inference)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("APP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    host = os.environ.get("DETECTION_HOST", "0.0.0.0")
    port = int(os.environ.get("DETECTION_PORT", "8002"))
    uvicorn.run(build_app(detector_factory=_factory), host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
