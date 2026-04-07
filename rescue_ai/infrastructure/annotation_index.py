"""Ground-truth annotation index built from COCO JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class AnnotationIndex:
    """Lookup index for ground-truth boxes by frame path variations.

    Matching strategies (tried in order):

    1. Full normalized path key relative to the mission workspace.
    2. Path key relative to ``frames_dir`` / without the ``images/`` prefix.
    3. Exact basename (case-sensitive) — requires basename to be unique.
    4. Case-insensitive stem (filename without extension) — catches
       ``frame_0001.jpeg`` in the COCO vs ``frame_0001.jpg`` on disk.
    5. Case-insensitive stem *suffix* — catches ``frame_0001`` in the COCO
       vs ``<mission-id>_frame_0001`` on disk when the prefix was added
       after annotations were produced.

    Strategies 4 and 5 only trigger when the match is *unambiguous* (the
    stem resolves to exactly one annotation entry). This avoids silently
    attributing the wrong bbox to a frame.
    """

    def __init__(
        self,
        frames_dir: Path,
        gt_boxes_by_key: dict[str, list[tuple[float, float, float, float]]],
        gt_boxes_by_unique_basename: dict[str, list[tuple[float, float, float, float]]],
        gt_boxes_by_unique_stem: dict[str, list[tuple[float, float, float, float]]],
    ) -> None:
        self._frames_dir = frames_dir.resolve()
        self._gt_boxes_by_key = gt_boxes_by_key
        self._gt_boxes_by_unique_basename = gt_boxes_by_unique_basename
        self._gt_boxes_by_unique_stem = gt_boxes_by_unique_stem

    def get_gt_boxes(self, frame_path: Path) -> list[tuple[float, float, float, float]]:
        for key in self._build_lookup_keys(frame_path):
            boxes = self._gt_boxes_by_key.get(key)
            if boxes is not None:
                return list(boxes)
        basename_hit = self._gt_boxes_by_unique_basename.get(frame_path.name)
        if basename_hit is not None:
            return list(basename_hit)
        return list(self._lookup_by_stem(frame_path))

    def has_frame(self, frame_path: Path) -> bool:
        if frame_path.name in self._gt_boxes_by_unique_basename:
            return True
        for key in self._build_lookup_keys(frame_path):
            if key in self._gt_boxes_by_key:
                return True
        return bool(self._lookup_by_stem(frame_path))

    def _lookup_by_stem(
        self, frame_path: Path
    ) -> list[tuple[float, float, float, float]]:
        stem = frame_path.stem.lower()
        if not stem:
            return []
        exact = self._gt_boxes_by_unique_stem.get(stem)
        if exact is not None:
            return exact
        # Suffix match: annotation stem is a suffix of the frame stem
        # (e.g. frame on disk is "<mission>_frame_0001", annotation is
        # "frame_0001"). Requires exactly one candidate.
        candidates = [
            boxes
            for ann_stem, boxes in self._gt_boxes_by_unique_stem.items()
            if stem.endswith(ann_stem) or ann_stem.endswith(stem)
        ]
        if len(candidates) == 1:
            return candidates[0]
        return []

    def _build_lookup_keys(self, frame_path: Path) -> list[str]:
        path = frame_path.resolve()
        keys: list[str] = []
        try:
            keys.append(_normalize_key(path.relative_to(self._frames_dir.parent)))
        except ValueError:
            pass
        try:
            keys.append(_normalize_key(path.relative_to(self._frames_dir)))
        except ValueError:
            pass
        keys.append(_normalize_key(Path(frame_path.name)))
        return keys


def build_annotation_index(
    frames_dir: Path,
    explicit_path: str | None,
) -> AnnotationIndex:
    source = _resolve_annotations_source(frames_dir=frames_dir, explicit=explicit_path)
    return _build_from_coco_json(coco_path=source, frames_dir=frames_dir)


def _resolve_annotations_source(frames_dir: Path, explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise ValueError(f"annotations source not found: {path}")
        if path.is_file():
            if path.suffix.lower() != ".json":
                raise ValueError("annotations file must be COCO .json")
            return path

        json_files = sorted(
            item for item in path.iterdir() if item.suffix.lower() == ".json"
        )
        if not json_files:
            raise ValueError("annotations dir must contain COCO .json file")
        return json_files[0]

    candidates = [
        frames_dir / "annotations",
        frames_dir.parent / "annotations",
    ]
    for candidate in candidates:
        if not candidate.exists() or not candidate.is_dir():
            continue
        json_files = sorted(
            path for path in candidate.iterdir() if path.suffix.lower() == ".json"
        )
        if json_files:
            return json_files[0]
    raise ValueError(
        "COCO annotations not found. Expected JSON in <mission>/annotations"
    )


def _build_from_coco_json(coco_path: Path, frames_dir: Path) -> AnnotationIndex:
    payload = json.loads(coco_path.read_text(encoding="utf-8"))
    image_keys_by_id, image_basename_by_id, basename_count = _extract_image_maps(
        _get_payload_rows(payload, "images")
    )
    person_category_ids = _extract_person_category_ids(
        _get_payload_rows(payload, "categories")
    )
    gt_boxes_by_key = _build_gt_boxes_by_key(
        annotations=_get_payload_rows(payload, "annotations"),
        image_keys_by_id=image_keys_by_id,
        person_category_ids=person_category_ids,
    )
    gt_boxes_by_unique_basename = _build_unique_basename_box_map(
        image_basename_by_id=image_basename_by_id,
        basename_count=basename_count,
        image_keys_by_id=image_keys_by_id,
        gt_boxes_by_key=gt_boxes_by_key,
    )
    gt_boxes_by_unique_stem = _build_unique_stem_box_map(
        image_basename_by_id=image_basename_by_id,
        image_keys_by_id=image_keys_by_id,
        gt_boxes_by_key=gt_boxes_by_key,
    )

    return AnnotationIndex(
        frames_dir=frames_dir,
        gt_boxes_by_key=gt_boxes_by_key,
        gt_boxes_by_unique_basename=gt_boxes_by_unique_basename,
        gt_boxes_by_unique_stem=gt_boxes_by_unique_stem,
    )


def _get_payload_rows(payload: Any, key: str) -> list[Any]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get(key, [])
    if not isinstance(rows, list):
        return []
    return rows


def _extract_image_maps(
    rows: list[Any],
) -> tuple[dict[int, list[str]], dict[int, str], dict[str, int]]:
    image_keys_by_id: dict[int, list[str]] = {}
    image_basename_by_id: dict[int, str] = {}
    basename_count: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        image_id = row.get("id")
        file_name = row.get("file_name")
        if isinstance(image_id, int) and isinstance(file_name, str):
            normalized = _normalize_key(Path(file_name))
            basename = Path(file_name).name
            keys = [normalized]
            tail = _normalize_without_images_prefix(normalized)
            if tail != normalized:
                keys.append(tail)
            image_keys_by_id[image_id] = keys
            image_basename_by_id[image_id] = basename
            basename_count[basename] = basename_count.get(basename, 0) + 1
    return image_keys_by_id, image_basename_by_id, basename_count


def _extract_person_category_ids(rows: list[Any]) -> set[int]:
    person_category_ids: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        category_id = row.get("id")
        category_name = row.get("name")
        name = str(category_name).strip().lower()
        if not isinstance(category_id, int):
            continue
        if name == "person" or "person" in name or "human" in name:
            person_category_ids.add(category_id)
    if not person_category_ids:
        person_category_ids = {1}
    return person_category_ids


def _append_coco_box(
    row: Any,
    gt_boxes_by_key: dict[str, list[tuple[float, float, float, float]]],
    image_keys_by_id: dict[int, list[str]],
    person_category_ids: set[int],
) -> None:
    if not isinstance(row, dict):
        return

    parsed = _parse_coco_annotation_row(row)
    if parsed is None:
        return

    image_id, category_id, bbox = parsed
    if person_category_ids and category_id not in person_category_ids:
        return

    image_keys = image_keys_by_id.get(image_id)
    if not image_keys:
        return

    converted = _xywh_to_xyxy(bbox)
    for key in image_keys:
        gt_boxes_by_key.setdefault(key, []).append(converted)


def _build_gt_boxes_by_key(
    annotations: list[Any],
    image_keys_by_id: dict[int, list[str]],
    person_category_ids: set[int],
) -> dict[str, list[tuple[float, float, float, float]]]:
    gt_boxes_by_key: dict[str, list[tuple[float, float, float, float]]] = {}
    for row in annotations:
        _append_coco_box(
            row=row,
            gt_boxes_by_key=gt_boxes_by_key,
            image_keys_by_id=image_keys_by_id,
            person_category_ids=person_category_ids,
        )
    return gt_boxes_by_key


def _build_unique_stem_box_map(
    image_basename_by_id: dict[int, str],
    image_keys_by_id: dict[int, list[str]],
    gt_boxes_by_key: dict[str, list[tuple[float, float, float, float]]],
) -> dict[str, list[tuple[float, float, float, float]]]:
    """Case-insensitive stem → boxes, populated only for stems that are unique.

    Two annotation images sharing the same stem (e.g. ``frame_0001.jpg`` and
    ``frame_0001.png``) are excluded to avoid ambiguous matches.
    """
    stem_count: dict[str, int] = {}
    stem_to_image_id: dict[str, int] = {}
    for image_id, basename in image_basename_by_id.items():
        stem = Path(basename).stem.lower()
        if not stem:
            continue
        stem_count[stem] = stem_count.get(stem, 0) + 1
        stem_to_image_id.setdefault(stem, image_id)
    result: dict[str, list[tuple[float, float, float, float]]] = {}
    for stem, count in stem_count.items():
        if count != 1:
            continue
        image_id = stem_to_image_id[stem]
        keys = image_keys_by_id.get(image_id, [])
        if not keys:
            continue
        result[stem] = gt_boxes_by_key.get(keys[0], [])
    return result


def _build_unique_basename_box_map(
    image_basename_by_id: dict[int, str],
    basename_count: dict[str, int],
    image_keys_by_id: dict[int, list[str]],
    gt_boxes_by_key: dict[str, list[tuple[float, float, float, float]]],
) -> dict[str, list[tuple[float, float, float, float]]]:
    gt_boxes_by_unique_basename: dict[str, list[tuple[float, float, float, float]]] = {}
    for image_id, basename in image_basename_by_id.items():
        if basename_count.get(basename, 0) != 1:
            continue
        keys = image_keys_by_id.get(image_id, [])
        if not keys:
            continue
        gt_boxes_by_unique_basename[basename] = gt_boxes_by_key.get(keys[0], [])
    return gt_boxes_by_unique_basename


def _parse_coco_annotation_row(
    row: dict[Any, Any],
) -> tuple[int, int, list[float | int]] | None:
    image_id_raw = row.get("image_id")
    category_id_raw = row.get("category_id")
    bbox = row.get("bbox")
    if (
        not isinstance(image_id_raw, (int, str))
        or not isinstance(category_id_raw, (int, str))
        or not isinstance(bbox, list)
        or len(bbox) != 4
    ):
        return None
    try:
        image_id = int(image_id_raw)
        category_id = int(category_id_raw)
    except ValueError:
        return None
    return image_id, category_id, bbox


def _xywh_to_xyxy(bbox: list[float | int]) -> tuple[float, float, float, float]:
    x, y, w, h = bbox
    return float(x), float(y), float(x + w), float(y + h)


def _normalize_key(path: Path) -> str:
    return str(path).replace("\\", "/").strip("./")


def _normalize_without_images_prefix(path_key: str) -> str:
    if path_key.startswith("images/"):
        return path_key.removeprefix("images/")
    return path_key
