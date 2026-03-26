"""Local filesystem mission source for batch evaluation."""

from __future__ import annotations

from pathlib import Path

from rescue_ai.application.annotation_index import build_annotation_index
from rescue_ai.application.batch_runner import FrameRecord, MissionInput

_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


class LocalMissionSource:
    """Loads dated mission frames from local directory structure."""

    def __init__(self, root_dir: Path, fps: float = 6.0) -> None:
        """Initialize with root directory and frame rate."""
        self._root_dir = root_dir
        self._fps = fps

    def load(self, mission_id: str, ds: str) -> MissionInput:
        """Load mission frames and annotations from a dated subdirectory."""
        mission_root = self._root_dir / mission_id / ds
        frames_dir = mission_root / "images"
        annotations_dir = mission_root / "annotations"

        frame_paths = sorted(
            item
            for item in frames_dir.glob("*")
            if item.is_file() and item.suffix.lower() in _ALLOWED_EXTENSIONS
        )
        if not frame_paths:
            raise ValueError(f"No frames found in {frames_dir}")

        gt_available = True
        annotation_index = None
        try:
            annotation_index = build_annotation_index(
                frames_dir=frames_dir,
                explicit_path=(
                    str(annotations_dir) if annotations_dir.exists() else None
                ),
            )
        except ValueError:
            gt_available = False

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
            source_uri=str(mission_root),
            frames=frames,
            gt_available=gt_available,
        )

    def describe_source(self) -> str:
        """Return a human-readable description of this source."""
        return f"local:{self._root_dir}"


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
