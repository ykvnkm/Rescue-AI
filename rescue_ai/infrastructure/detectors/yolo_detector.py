"""YOLOv8 detector wrappers for PT and NCNN runtimes."""

from __future__ import annotations

import importlib
import logging
import time
from pathlib import Path
from typing import Any

from rescue_ai.application.inference_config import InferenceConfig
from rescue_ai.domain.entities import Detection
from rescue_ai.infrastructure.model_cache import ModelCache

logger = logging.getLogger(__name__)


def _load_yolo_class():
    return getattr(importlib.import_module("ultralytics"), "YOLO")


class _BaseYoloDetector:
    """Shared Ultralytics-backed detector implementation."""

    def __init__(
        self,
        config: InferenceConfig,
        *,
        model_version: str,
        cache: ModelCache | None = None,
    ) -> None:
        self._config = config
        self._model_version = model_version
        self._cache = cache or ModelCache()
        self._model: Any | None = None

    def detect(self, image_uri: object) -> list[Detection]:
        """Run detection on a single frame and return normalized detections."""
        t0 = time.perf_counter()
        results = self._predict_raw(image_uri)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        if not results:
            logger.debug("YOLO inference: no results (%.1f ms)", elapsed_ms)
            return []

        result = results[0]
        detections = _extract_detections(
            result=result,
            confidence_threshold=self._config.confidence_threshold,
            model_name=self._model_version,
        )
        logger.debug(
            "YOLO inference: detections=%d elapsed=%.1f ms conf_threshold=%.3f",
            len(detections),
            elapsed_ms,
            self._config.confidence_threshold,
        )
        return detections

    def runtime_name(self) -> str:
        """Return human-readable runtime name."""
        return self._config.runtime

    def _predict_raw(self, image_source: object):
        model = self._ensure_model()
        source = self._resolve_predict_source(image_source)
        return model.predict(
            source=source,
            conf=self._config.confidence_threshold,
            iou=self._config.nms_iou,
            imgsz=self._config.imgsz,
            max_det=self._config.max_det,
            device=self._config.device,
            verbose=False,
        )

    def _resolve_predict_source(self, image_source: object) -> object:
        if isinstance(image_source, Path):
            return str(image_source)
        if isinstance(image_source, str):
            return image_source

        try:
            import numpy as np
        except ImportError as exc:
            raise TypeError("numpy is required for in-memory detection source") from exc

        if isinstance(image_source, np.ndarray):
            return image_source

        if isinstance(image_source, bytes):
            try:
                import cv2
            except ImportError as exc:
                raise TypeError(
                    "opencv-python is required for bytes detection source"
                ) from exc
            frame = cv2.imdecode(
                np.frombuffer(image_source, dtype=np.uint8), cv2.IMREAD_COLOR
            )
            if frame is None:
                raise ValueError("Failed to decode JPEG bytes for detection")
            return frame

        raise TypeError(f"Unsupported image source type: {type(image_source)!r}")

    def warmup(self) -> None:
        self._ensure_model()

    def _ensure_model(self):
        if self._model is not None:
            return self._model

        try:
            yolo_cls = _load_yolo_class()
        except (ImportError, AttributeError) as error:
            raise RuntimeError(
                "ultralytics is not installed.\n"
                "Install: uv sync --extra inference --extra dev"
            ) from error

        model_path = self._resolve_model_path()
        checksum_status = "verified" if self._config.model_sha256 else "skipped"
        logger.info("Model loaded: path=%s checksum=%s", model_path, checksum_status)
        self._model = yolo_cls(str(model_path))
        return self._model

    def _resolve_model_path(self) -> Path:
        raise NotImplementedError


class PtYoloDetector(_BaseYoloDetector):
    """YOLO detector that loads PyTorch weights for stand processing."""

    def __init__(self, config: InferenceConfig, model_version: str = "yolov8n-pt"):
        super().__init__(config, model_version=model_version)

    def _resolve_model_path(self) -> Path:
        return self._cache.resolve_file(
            self._config.pt_model_url,
            self._config.pt_model_sha256,
        )


class NcnnYoloDetector(_BaseYoloDetector):
    """YOLO detector that loads an exported NCNN package for local streaming."""

    def __init__(self, config: InferenceConfig, model_version: str = "yolov8n-ncnn"):
        super().__init__(config, model_version=model_version)

    def _resolve_model_path(self) -> Path:
        if not self._config.ncnn_model_url:
            raise RuntimeError("NCNN runtime requires ncnn_model_url")
        resolved = self._cache.resolve_directory(
            self._config.ncnn_model_url,
            self._config.ncnn_model_sha256,
        )
        return _ensure_ncnn_model_dirname(resolved)


_NCNN_PARAM_FILE = "model.ncnn.param"


def _find_ncnn_model_dir(path: Path) -> Path:
    """Locate the directory that actually holds the NCNN weights.

    A package exported to ``*_ncnn`` and zipped on macOS unpacks to a doubly
    nested layout — the outer ``*_ncnn`` dir contains a ``*_ncnn_model`` subdir
    with the real ``model.ncnn.param``/``.bin`` plus a junk ``__MACOSX`` sibling.
    Ultralytics must be pointed at the directory that *directly* contains
    ``model.ncnn.param``; aiming it at the outer dir loads an empty model whose
    inference yields nothing (surfacing as a cryptic ``StopIteration``).
    """
    if (path / _NCNN_PARAM_FILE).is_file():
        return path
    nested = sorted(
        child
        for child in path.glob("*")
        if child.is_dir()
        and child.name != "__MACOSX"
        and (child / _NCNN_PARAM_FILE).is_file()
    )
    if nested:
        return nested[0]
    # Nothing matched — return the original path so Ultralytics raises a clear
    # "not a supported model format" instead of us guessing.
    return path


def _ensure_ncnn_model_dirname(path: Path) -> Path:
    """Expose the NCNN package under a ``*_ncnn_model`` directory name.

    Ultralytics detects the NCNN format by a directory whose name ends with
    ``_ncnn_model``; packages exported as ``*_ncnn`` (as on our model registry)
    are otherwise rejected with 'not a supported model format'. We first resolve
    the real weights directory (handling the doubly nested export layout), then
    — if it is not already named ``*_ncnn_model`` — expose a correctly named
    symlink rather than renaming the (possibly read-only) cached package.
    """
    model_dir = _find_ncnn_model_dir(path)
    if model_dir.name.endswith("_ncnn_model"):
        return model_dir
    import tempfile  # noqa: PLC0415

    # The symlink target MUST be absolute: ModelCache returns a relative path
    # (``runtime/models/...``) and a relative symlink under /tmp would dangle.
    target = model_dir.resolve()
    name = model_dir.name
    base = name[: -len("_ncnn")] if name.endswith("_ncnn") else name
    link = Path(tempfile.gettempdir()) / "rescue_ai_ncnn" / f"{base}_ncnn_model"
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            link.unlink()  # drop a possibly-stale link, recreate fresh
        if not link.exists():
            link.symlink_to(target, target_is_directory=True)
        return link
    except OSError:
        logger.warning("NCNN symlink failed for %s; passing raw path", model_dir)
        return model_dir


YoloDetector = PtYoloDetector


def _extract_detections(
    result, confidence_threshold: float, model_name: str = "yolo8n"
) -> list[Detection]:
    boxes = result.boxes
    names = result.names

    if boxes is None:
        return []

    person_ids = _resolve_person_ids(names)
    cls_ids = boxes.cls.cpu().numpy().astype(int)
    scores = boxes.conf.cpu().numpy()
    coords = boxes.xyxy.cpu().numpy()

    detections: list[Detection] = []
    for box, score, cls_id in zip(coords, scores, cls_ids):
        if person_ids and cls_id not in person_ids:
            continue
        if float(score) < confidence_threshold:
            continue

        detections.append(
            Detection(
                bbox=(
                    float(box[0]),
                    float(box[1]),
                    float(box[2]),
                    float(box[3]),
                ),
                score=float(score),
                label="person",
                model_name=model_name,
            )
        )

    return detections


def _resolve_person_ids(names: dict[int, str] | list[str]) -> set[int]:
    if isinstance(names, dict):
        return {idx for idx, name in names.items() if str(name).lower() == "person"}

    return {idx for idx, name in enumerate(names) if str(name).lower() == "person"}
