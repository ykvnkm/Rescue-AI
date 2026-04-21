"""Architecture tests: ``rescue_ai/infrastructure/video/`` import boundaries."""

from __future__ import annotations

from pathlib import Path

from tests.architecture.import_boundaries import collect_import_violations

VIDEO_DIR = Path("rescue_ai/infrastructure/video")


def test_video_does_not_import_domain() -> None:
    violations = collect_import_violations(
        target_dir=VIDEO_DIR,
        forbidden_prefix="rescue_ai.domain",
    )
    assert not violations


def test_video_does_not_import_application() -> None:
    violations = collect_import_violations(
        target_dir=VIDEO_DIR,
        forbidden_prefix="rescue_ai.application",
    )
    assert not violations


def test_video_does_not_import_interfaces() -> None:
    violations = collect_import_violations(
        target_dir=VIDEO_DIR,
        forbidden_prefix="rescue_ai.interfaces",
    )
    assert not violations
