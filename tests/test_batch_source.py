from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from rescue_ai.infrastructure.local_mission_source import LocalMissionSource


def test_local_mission_source_marks_corrupted_images() -> None:
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        images_dir = root / "mission-1" / "2026-03-01" / "images"
        images_dir.mkdir(parents=True)

        valid = images_dir / "frame_0001.jpg"
        valid.write_bytes(
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xd9"
        )
        broken = images_dir / "frame_0002.jpg"
        broken.write_bytes(b"not-an-image")

        source = LocalMissionSource(root_dir=root, fps=2.0)
        mission_input = source.load(
            mission_id="mission-1",
            ds="2026-03-01",
        )

    assert len(mission_input.frames) == 2
    assert mission_input.frames[0].is_corrupted is False
    assert mission_input.frames[1].is_corrupted is True
