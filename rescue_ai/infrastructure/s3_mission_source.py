"""S3-backed mission source for batch evaluation."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from tempfile import mkdtemp

from rescue_ai.application.batch_dtos import FrameRecord, MissionInput
from rescue_ai.infrastructure.annotation_index import build_annotation_index
from rescue_ai.infrastructure.artifact_storage import S3ArtifactBackendSettings

_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
GLOBAL_MISSION_ID = "__all_missions__"


class S3MissionSource:
    """Loads mission frames from an S3-compatible bucket into a temp workspace."""

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
        """Load one mission/day dataset from S3 and stage to local temp files."""
        if mission_id.strip() == GLOBAL_MISSION_ID:
            return self._load_global(ds)

        source_key_root = self._resolve_source_root(mission_id=mission_id, ds=ds)
        frame_keys = self._list_frame_keys(f"{source_key_root}/images/")
        if not frame_keys:
            raise ValueError(
                "No frame images found in "
                f"s3://{self._bucket}/{source_key_root}/images/"
            )

        mission_workspace = self._workspace / mission_id / ds
        frames_dir = mission_workspace / "images"
        frames_dir.mkdir(parents=True, exist_ok=True)
        self._download_objects(frame_keys, frames_dir)

        annotation_keys = self._list_json_keys(f"{source_key_root}/annotations/")
        annotations_dir = mission_workspace / "annotations"
        if annotation_keys:
            annotations_dir.mkdir(parents=True, exist_ok=True)
            self._download_objects(annotation_keys, annotations_dir)

        return self._build_mission_input(
            source_key_root=source_key_root,
            frames_dir=frames_dir,
            annotations_dir=annotations_dir,
            has_annotations=bool(annotation_keys),
        )

    def describe_source(self) -> str:
        """Return human-readable source description."""
        return f"s3://{self._bucket}/{self._source_prefix}"

    def _build_mission_input(
        self,
        *,
        source_key_root: str,
        frames_dir: Path,
        annotations_dir: Path,
        has_annotations: bool,
    ) -> MissionInput:
        gt_available = True
        annotation_index = None
        try:
            annotation_index = build_annotation_index(
                frames_dir=frames_dir,
                explicit_path=str(annotations_dir) if has_annotations else None,
            )
        except ValueError:
            gt_available = False

        frame_paths = sorted(
            item
            for item in frames_dir.glob("*")
            if item.is_file() and item.suffix.lower() in _ALLOWED_EXTENSIONS
        )
        frames: list[FrameRecord] = []
        for idx, frame_path in enumerate(frame_paths, start=1):
            gt_boxes = (
                annotation_index.get_gt_boxes(frame_path) if annotation_index else []
            )
            frames.append(
                FrameRecord(
                    frame_id=idx,
                    ts_sec=(idx - 1) / self._fps,
                    frame_path=frame_path,
                    image_uri=str(frame_path),
                    gt_person_present=bool(gt_boxes),
                    is_corrupted=_is_corrupted_image(frame_path),
                )
            )

        return MissionInput(
            source_uri=f"s3://{self._bucket}/{source_key_root}",
            frames=frames,
            gt_available=gt_available,
        )

    def _load_global(self, ds: str) -> MissionInput:
        roots = self._discover_source_roots_up_to_ds(ds)
        if not roots:
            raise ValueError(
                "No frame images found in "
                f"s3://{self._bucket}/{self._source_prefix} up to ds={ds}"
            )

        aggregated: list[FrameRecord] = []
        gt_available = True
        for root in roots:
            frame_keys = self._list_frame_keys(f"{root}/images/")
            if not frame_keys:
                continue
            annotation_keys = self._list_json_keys(f"{root}/annotations/")

            workspace_key = root.replace("/", "__")
            mission_workspace = self._workspace / GLOBAL_MISSION_ID / ds / workspace_key
            frames_dir = mission_workspace / "images"
            frames_dir.mkdir(parents=True, exist_ok=True)
            self._download_objects(frame_keys, frames_dir)

            annotations_dir = mission_workspace / "annotations"
            if annotation_keys:
                annotations_dir.mkdir(parents=True, exist_ok=True)
                self._download_objects(annotation_keys, annotations_dir)

            mission_input = self._build_mission_input(
                source_key_root=root,
                frames_dir=frames_dir,
                annotations_dir=annotations_dir,
                has_annotations=bool(annotation_keys),
            )
            gt_available = gt_available and mission_input.gt_available
            aggregated.extend(mission_input.frames)

        ordered = sorted(aggregated, key=lambda frame: str(frame.frame_path))
        frames: list[FrameRecord] = []
        for idx, frame in enumerate(ordered, start=1):
            frames.append(
                FrameRecord(
                    frame_id=idx,
                    ts_sec=(idx - 1) / self._fps,
                    frame_path=frame.frame_path,
                    image_uri=frame.image_uri,
                    gt_person_present=frame.gt_person_present,
                    is_corrupted=frame.is_corrupted,
                )
            )

        return MissionInput(
            source_uri=f"s3://{self._bucket}/{self._source_prefix}/ds<={ds}",
            frames=frames,
            gt_available=gt_available,
        )

    def _discover_source_roots_up_to_ds(self, ds: str) -> list[str]:
        frame_keys = self._list_frame_keys(self._join(self._source_prefix))
        roots: set[str] = set()
        for key in frame_keys:
            parsed = self._parse_source_root_with_ds(key)
            if parsed is None:
                continue
            source_root, key_ds = parsed
            if _date_not_after(key_ds, ds):
                roots.add(source_root)
        return sorted(roots)

    def _parse_source_root_with_ds(self, key: str) -> tuple[str, str] | None:
        if "/images/" not in key:
            return None
        source_root, _ = key.split("/images/", 1)
        relative_root = source_root
        source_prefix_with_sep = (
            f"{self._source_prefix}/" if self._source_prefix else ""
        )
        if source_prefix_with_sep and source_root.startswith(source_prefix_with_sep):
            relative_root = source_root.removeprefix(source_prefix_with_sep)

        parts = [part for part in relative_root.split("/") if part]
        if len(parts) < 2:
            return None
        last_part = parts[-1]
        ds = (
            last_part.split("=", 1)[1]
            if last_part.startswith("ds=")
            else last_part
        )
        if not _is_iso_date(ds):
            return None
        return source_root, ds

    def _resolve_source_root(self, mission_id: str, ds: str) -> str:
        candidates = [
            self._join(self._source_prefix, f"mission={mission_id}", f"ds={ds}"),
            self._join(self._source_prefix, mission_id, ds),
        ]
        for prefix in candidates:
            if self._list_keys(prefix=f"{prefix}/", max_keys=1):
                return prefix
        return candidates[0]

    def _list_keys(self, prefix: str, max_keys: int | None = None) -> list[str]:
        kwargs: dict[str, object] = {"Bucket": self._bucket, "Prefix": prefix}
        if max_keys is not None:
            kwargs["MaxKeys"] = max_keys
        response = self._client.list_objects_v2(**kwargs)
        return [item["Key"] for item in response.get("Contents", [])]

    def _list_frame_keys(self, prefix: str) -> list[str]:
        keys = self._list_keys(prefix)
        return sorted(
            key for key in keys if Path(key).suffix.lower() in _ALLOWED_EXTENSIONS
        )

    def _list_json_keys(self, prefix: str) -> list[str]:
        keys = self._list_keys(prefix)
        return sorted(key for key in keys if key.lower().endswith(".json"))

    def _download_objects(self, keys: list[str], target_dir: Path) -> None:
        for key in keys:
            target = target_dir / Path(key).name
            self._client.download_file(self._bucket, key, str(target))

    @staticmethod
    def _join(*parts: str) -> str:
        return "/".join(part.strip("/") for part in parts if part.strip("/"))


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


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _date_not_after(candidate: str, target: str) -> bool:
    return date.fromisoformat(candidate) <= date.fromisoformat(target)
