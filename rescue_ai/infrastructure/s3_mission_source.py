"""S3-backed mission source for the batch ML pipeline.

Reads one mission/day dataset from the canonical date-partitioned layout::

    {prefix}/YYYY-MM-DD/{mission_id}/frames/<frame>.jpg
    {prefix}/YYYY-MM-DD/{mission_id}/labels.json

Frames and labels are decoupled on purpose: frames land in real time as
the drone uploads them, labels arrive later (operator review of alerts
or asynchronous manual annotation). Both ride the same ``ds`` partition
so a daily rerun of the DAG sees a consistent snapshot.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from botocore.exceptions import ClientError

from rescue_ai.application.batch_dtos import FrameRecord, MissionInput
from rescue_ai.infrastructure.artifact_storage import S3ArtifactBackendSettings

_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


class S3MissionSource:
    """Loads mission frames + labels from S3 into a temp workspace."""

    def __init__(
        self,
        settings: S3ArtifactBackendSettings,
        *,
        source_prefix: str,
        fps: float = 6.0,
    ) -> None:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("boto3 is required for S3 mission source") from exc

        self._source_prefix = source_prefix.strip("/")
        self._fps = fps
        self._bucket = settings.bucket
        self._workspace = Path(mkdtemp(prefix="rescue_ai_batch_source_"))
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.endpoint,
            region_name=settings.region,
            aws_access_key_id=settings.access_key_id,
            aws_secret_access_key=settings.secret_access_key,
        )

    def load(self, mission_id: str, ds: str) -> MissionInput:
        """Load one mission/day dataset from S3 into a local temp workspace."""
        source_root = self._mission_root(mission_id=mission_id, ds=ds)
        frame_keys = self._list_frame_keys(f"{source_root}/frames/")
        if not frame_keys:
            raise ValueError(
                f"No frame images found in s3://{self._bucket}/{source_root}/frames/"
            )

        mission_workspace = self._workspace / ds / mission_id
        frames_dir = mission_workspace / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        self._download_objects(frame_keys, frames_dir)

        labels_key = f"{source_root}/labels.json"
        labels = self._load_labels(labels_key)
        gt_available = labels is not None

        frame_paths = sorted(
            item
            for item in frames_dir.glob("*")
            if item.is_file() and item.suffix.lower() in _ALLOWED_EXTENSIONS
        )
        frames = self._build_frames(frame_paths, source_root=source_root, labels=labels)

        return MissionInput(
            source_uri=f"s3://{self._bucket}/{source_root}",
            frames=frames,
            gt_available=gt_available,
        )

    def describe_source(self) -> str:
        """Return human-readable source description."""
        return f"s3://{self._bucket}/{self._source_prefix}"

    def _mission_root(self, mission_id: str, ds: str) -> str:
        return self._join(self._source_prefix, ds, mission_id)

    def _list_keys(self, prefix: str) -> list[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for item in page.get("Contents", []) or []:
                keys.append(item["Key"])
        return keys

    def _list_frame_keys(self, prefix: str) -> list[str]:
        keys = self._list_keys(prefix)
        return sorted(
            key for key in keys if Path(key).suffix.lower() in _ALLOWED_EXTENSIONS
        )

    def _load_labels(self, key: str) -> dict[str, object] | None:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except (ClientError, KeyError):
            return None
        try:
            payload = json.loads(response["Body"].read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _download_objects(self, keys: list[str], target_dir: Path) -> None:
        for key in keys:
            target = target_dir / Path(key).name
            self._client.download_file(self._bucket, key, str(target))

    def _build_frames(
        self,
        frame_paths: list[Path],
        *,
        source_root: str,
        labels: dict[str, Any] | None,
    ) -> list[FrameRecord]:
        frames: list[FrameRecord] = []
        coco_positive_filenames = _coco_person_positive_filenames(labels)
        for idx, frame_path in enumerate(frame_paths, start=1):
            if coco_positive_filenames is not None:
                gt_present = frame_path.name in coco_positive_filenames
            else:
                gt_present = (
                    bool(_label_for(labels, frame_path.name)) if labels else False
                )
            s3_uri = f"s3://{self._bucket}/{source_root}/frames/{frame_path.name}"
            frames.append(
                FrameRecord(
                    frame_id=idx,
                    ts_sec=(idx - 1) / self._fps,
                    frame_path=frame_path,
                    image_uri=s3_uri,
                    gt_person_present=gt_present,
                    is_corrupted=_is_corrupted_image(frame_path),
                )
            )
        return frames

    @staticmethod
    def _join(*parts: str) -> str:
        return "/".join(part.strip("/") for part in parts if part.strip("/"))


def _label_for(labels: dict[str, object] | None, filename: str) -> bool:
    """Return whether the labels blob marks a given frame as positive.

    Supports two simple shapes for ``labels.json``:

    * Flat ``{"frame_001.jpg": true, "frame_002.jpg": false}``
    * Nested ``{"frame_001.jpg": {"gt_person_present": true}, ...}``
    """
    if not labels:
        return False
    entry = labels.get(filename)
    if entry is None:
        return False
    if isinstance(entry, bool):
        return entry
    if isinstance(entry, dict):
        return bool(entry.get("gt_person_present"))
    return False


def _coco_person_positive_filenames(
    labels: dict[str, object] | None,
) -> set[str] | None:
    """Return positive frame filenames if labels follow COCO shape.

    Returns ``None`` when payload is not COCO-like, so callers can fallback
    to legacy per-file mapping logic.
    """
    if not isinstance(labels, dict):
        return None

    raw_images = labels.get("images")
    raw_annotations = labels.get("annotations")
    if not isinstance(raw_images, list) or not isinstance(raw_annotations, list):
        return None

    image_id_to_name: dict[int, str] = {}
    for item in raw_images:
        if not isinstance(item, dict):
            continue
        image_id = item.get("id")
        file_name = item.get("file_name")
        if isinstance(image_id, bool) or not isinstance(image_id, int):
            continue
        if not isinstance(file_name, str) or not file_name.strip():
            continue
        image_id_to_name[image_id] = Path(file_name).name

    if not image_id_to_name:
        return set()

    person_category_ids = _coco_person_category_ids(labels.get("categories"))
    positive_image_ids: set[int] = set()
    for item in raw_annotations:
        if not isinstance(item, dict):
            continue
        image_id = item.get("image_id")
        if isinstance(image_id, bool) or not isinstance(image_id, int):
            continue
        if image_id not in image_id_to_name:
            continue
        category_id = item.get("category_id")
        if not _matches_person_category(category_id, person_category_ids):
            continue
        positive_image_ids.add(image_id)

    return {image_id_to_name[image_id] for image_id in positive_image_ids}


def _coco_person_category_ids(raw_categories: object) -> set[int] | None:
    """Resolve COCO category ids for class name ``person``.

    Returns ``None`` when categories are absent/invalid, meaning "do not
    filter by category id".
    """
    if not isinstance(raw_categories, list):
        return None

    person_ids: set[int] = set()
    for item in raw_categories:
        if not isinstance(item, dict):
            continue
        category_id = item.get("id")
        name = item.get("name")
        if isinstance(category_id, bool) or not isinstance(category_id, int):
            continue
        if isinstance(name, str) and name.strip().lower() == "person":
            person_ids.add(category_id)

    return person_ids if person_ids else None


def _matches_person_category(
    category_id: object, person_category_ids: set[int] | None
) -> bool:
    """Return True when annotation category passes the optional person filter."""
    if person_category_ids is None:
        return True
    if isinstance(category_id, bool) or not isinstance(category_id, int):
        return False
    return category_id in person_category_ids


def _is_corrupted_image(frame_path: Path) -> bool:
    """Check whether a file has a valid image magic-byte header."""
    header = frame_path.read_bytes()[:16]
    if len(header) < 2:
        return True

    is_jpeg = header.startswith(b"\xff\xd8")
    is_png = header.startswith(b"\x89PNG\r\n\x1a\n")
    is_bmp = header.startswith(b"BM")
    is_webp = (
        len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP"
    )
    return not any([is_jpeg, is_png, is_bmp, is_webp])
