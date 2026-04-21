"""NanoDet-Plus detector wrapper (pth-first, onnx fallback).

Mirrors :class:`YoloDetector`'s ``DetectorPort`` shape (``detect``/``warmup``/
``runtime_name``) but runs the vendored NanoDet inference core at
``rescue_ai.infrastructure.detectors.nanodet_core``.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlretrieve

import numpy as np

from rescue_ai.application.inference_config import InferenceConfig
from rescue_ai.domain.entities import Detection

MODEL_CACHE_DIR = Path("runtime/models")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NanoDetSettings:
    """NanoDet-specific runtime settings — model topology config + weights."""

    weights_url: str
    config_url: str
    weights_sha256: str | None = None
    config_sha256: str | None = None
    onnx_url: str | None = None
    onnx_sha256: str | None = None


class NanoDetDetector:
    """NanoDet-Plus detector with lazy model load, pth primary, onnx fallback."""

    def __init__(
        self,
        config: InferenceConfig,
        settings: NanoDetSettings,
        model_version: str = "nanodet-plus-m-1.5x-416",
    ) -> None:
        self._config = config
        self._settings = settings
        self._model_version = model_version
        self._state: dict[str, Any] | None = None

    def detect(self, image_uri: object) -> list[Detection]:
        """Run detection on a single frame and return normalized detections."""
        t0 = time.perf_counter()
        frame_bgr = self._resolve_frame(image_uri)
        state = self._ensure_model()
        raw_boxes = self._run_inference(frame_bgr, state)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        detections = _build_detections(
            raw_boxes=raw_boxes,
            confidence_threshold=self._config.confidence_threshold,
            model_name=self._model_version,
        )
        logger.debug(
            (
                "NanoDet inference: detections=%d elapsed=%.1f ms "
                "backend=%s conf_threshold=%.3f"
            ),
            len(detections),
            elapsed_ms,
            state["backend"],
            self._config.confidence_threshold,
        )
        return detections

    def warmup(self) -> None:
        self._ensure_model()

    def runtime_name(self) -> str:
        return "nanodet"

    def _ensure_model(self) -> dict[str, Any]:
        if self._state is not None:
            return self._state

        try:
            import torch
        except ImportError as error:
            raise RuntimeError(
                "torch is not installed.\n"
                "Install: uv sync --extra inference --extra dev"
            ) from error

        from rescue_ai.infrastructure.detectors.nanodet_core.data.batch_process import (
            stack_batch_img,
        )
        from rescue_ai.infrastructure.detectors.nanodet_core.data.collate import (
            naive_collate,
        )
        from rescue_ai.infrastructure.detectors.nanodet_core.data.transform import (
            Pipeline,
        )
        from rescue_ai.infrastructure.detectors.nanodet_core.model.arch import (
            build_model,
        )
        from rescue_ai.infrastructure.detectors.nanodet_core.util import (
            cfg as nanodet_cfg,
        )
        from rescue_ai.infrastructure.detectors.nanodet_core.util import (
            load_config,
            load_model_weight,
        )

        MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        config_path = _ensure_asset(
            self._settings.config_url,
            sha256=self._settings.config_sha256,
            label="NanoDet config",
        )
        load_config(nanodet_cfg, str(config_path))

        model = build_model(nanodet_cfg.model)
        device = torch.device(self._config.device)

        pth_loaded = False
        try:
            weights_path = _ensure_asset(
                self._settings.weights_url,
                sha256=self._settings.weights_sha256,
                label="NanoDet weights (pth)",
            )
            checkpoint = torch.load(
                weights_path,
                map_location=lambda storage, loc: storage,
                weights_only=False,
            )
            load_model_weight(model, checkpoint, _PrintLogger())
            pth_loaded = True
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            if not self._settings.onnx_url:
                raise
            logger.warning(
                "Failed to load NanoDet pth weights (%s); trying onnx fallback",
                exc,
            )

        if pth_loaded and nanodet_cfg.model.arch.backbone.name == "RepVGG":
            from rescue_ai.infrastructure.detectors.nanodet_core.model.backbone import (
                repvgg as repvgg_backbone,
            )

            deploy_cfg = nanodet_cfg.model
            deploy_cfg.arch.backbone.update({"deploy": True})
            deploy_model = build_model(deploy_cfg)
            model = repvgg_backbone.repvgg_det_model_convert(model, deploy_model)

        model = model.to(device).eval()

        pipeline = Pipeline(
            nanodet_cfg.data.val.pipeline,
            nanodet_cfg.data.val.keep_ratio,
        )
        class_names = list(nanodet_cfg.class_names) if nanodet_cfg.class_names else []
        person_idx = class_names.index("person") if "person" in class_names else 0

        session = None
        input_name: str | None = None
        backend = "torch"
        if self._settings.onnx_url and (not pth_loaded):
            session, input_name = _try_load_onnx_session(
                onnx_url=self._settings.onnx_url,
                onnx_sha256=self._settings.onnx_sha256,
                device=device,
            )
            if session is not None:
                backend = "onnx"

        if not pth_loaded and session is None:
            raise RuntimeError(
                "NanoDet: neither pth weights nor onnx fallback are available"
            )

        self._state = {
            "model": model,
            "device": device,
            "pipeline": pipeline,
            "input_size": tuple(nanodet_cfg.data.val.input_size),
            "stack_batch_img": stack_batch_img,
            "naive_collate": naive_collate,
            "person_idx": person_idx,
            "num_classes": model.head.num_classes,
            "head": model.head,
            "backend": backend,
            "onnx_session": session,
            "onnx_input_name": input_name,
            "torch": torch,
        }
        logger.info(
            "NanoDet loaded: weights=%s backend=%s device=%s",
            weights_path,
            backend,
            device,
        )
        return self._state

    def _run_inference(
        self,
        frame_bgr: np.ndarray,
        state: dict[str, Any],
    ) -> dict[int, list[list[float]]]:
        torch = state["torch"]
        pipeline = state["pipeline"]
        input_size = state["input_size"]
        device = state["device"]

        img_info = {
            "id": 0,
            "file_name": None,
            "height": frame_bgr.shape[0],
            "width": frame_bgr.shape[1],
        }
        meta: dict[str, Any] = {
            "img_info": img_info,
            "raw_img": frame_bgr,
            "img": frame_bgr,
        }
        meta = pipeline(None, meta, input_size)
        img_np = np.asarray(meta["img"])
        meta["img"] = torch.from_numpy(img_np.transpose(2, 0, 1)).to(device)
        meta = state["naive_collate"]([meta])
        meta["img"] = state["stack_batch_img"](meta["img"], divisible=32)

        if state["backend"] == "onnx":
            session = state["onnx_session"]
            input_name = state["onnx_input_name"]
            head = state["head"]
            num_classes = state["num_classes"]
            input_tensor = meta["img"]
            input_data = input_tensor.cpu().numpy().astype(np.float32, copy=False)
            output = session.run(None, {input_name: input_data})
            if not output:
                return {}
            preds = torch.from_numpy(output[0]).float()
            cls_scores = preds[..., :num_classes]
            cls_logits = torch.logit(cls_scores.clamp(1e-6, 1 - 1e-6))
            preds_for_post = torch.cat([cls_logits, preds[..., num_classes:]], dim=-1)
            with torch.no_grad():
                results = head.post_process(preds_for_post, meta)
        else:
            with torch.no_grad():
                results = state["model"].inference(meta)

        if not isinstance(results, dict) or not results:
            return {}
        if 0 in results:
            entry = results[0]
        else:
            entry = next(iter(results.values()))
        return entry if isinstance(entry, dict) else {}

    def _resolve_frame(self, image_source: object) -> np.ndarray:
        if isinstance(image_source, np.ndarray):
            return image_source
        if isinstance(image_source, (str, Path)):
            import cv2

            frame = cv2.imread(str(image_source))
            if frame is None:
                raise ValueError(f"Failed to read image: {image_source}")
            return frame
        if isinstance(image_source, bytes):
            import cv2

            frame = cv2.imdecode(
                np.frombuffer(image_source, dtype=np.uint8), cv2.IMREAD_COLOR
            )
            if frame is None:
                raise ValueError("Failed to decode JPEG bytes for detection")
            return frame
        raise TypeError(f"Unsupported image source type: {type(image_source)!r}")


class _PrintLogger:
    """Minimal logger protocol required by nanodet_core.load_model_weight."""

    def log(self, msg: str) -> None:
        logger.info("nanodet: %s", msg)


def _ensure_asset(source: str, *, sha256: str | None, label: str) -> Path:
    parsed = urlparse(source)
    if parsed.scheme in ("", "file"):
        local_path = Path(parsed.path if parsed.scheme == "file" else source)
        if not local_path.exists():
            raise FileNotFoundError(f"{label}: local file not found: {local_path}")
        _verify_checksum(local_path, sha256, label=label)
        return local_path

    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    filename = Path(parsed.path).name
    if not filename:
        raise ValueError(f"{label}: cannot derive filename from URL {source!r}")
    target = MODEL_CACHE_DIR / filename
    if not target.exists():
        logger.info("Downloading %s: %s → %s", label, source, target)
        urlretrieve(source, target)
        logger.info("%s downloaded: %s", label, target)
    else:
        logger.info("%s cache hit: %s", label, target)
    _verify_checksum(target, sha256, label=label)
    return target


def _verify_checksum(path: Path, expected: str | None, *, label: str) -> None:
    if not expected:
        return
    normalized = expected.strip().lower()
    if len(normalized) != 64 or not all(ch in "0123456789abcdef" for ch in normalized):
        raise RuntimeError(f"Invalid {label} sha256 format in runtime config")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != normalized:
        raise RuntimeError(
            f"{label} checksum mismatch for {path.name}: "
            f"expected {normalized}, got {actual}"
        )


def _try_load_onnx_session(
    *,
    onnx_url: str,
    onnx_sha256: str | None,
    device: Any,
) -> tuple[Any, str | None]:
    try:
        import onnxruntime as ort
    except ImportError:
        logger.warning("onnxruntime is not installed; falling back to torch inference")
        return None, None
    try:
        onnx_path = _ensure_asset(
            onnx_url, sha256=onnx_sha256, label="NanoDet weights (onnx)"
        )
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        logger.warning("Failed to fetch NanoDet ONNX weights (%s); using torch", exc)
        return None, None
    providers = ["CPUExecutionProvider"]
    if getattr(device, "type", "cpu") == "cuda":
        available = set(ort.get_available_providers())
        if "CUDAExecutionProvider" in available:
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    session = ort.InferenceSession(str(onnx_path), providers=providers)
    input_name = session.get_inputs()[0].name
    return session, input_name


def _build_detections(
    *,
    raw_boxes: dict[int, list[list[float]]],
    confidence_threshold: float,
    model_name: str,
) -> list[Detection]:
    detections: list[Detection] = []
    for _cls_idx, boxes in raw_boxes.items():
        for box in boxes:
            if len(box) < 5:
                continue
            x1, y1, x2, y2, score = box[:5]
            if float(score) < confidence_threshold:
                continue
            detections.append(
                Detection(
                    bbox=(float(x1), float(y1), float(x2), float(y2)),
                    score=float(score),
                    label="person",
                    model_name=model_name,
                )
            )
    return detections
