"""Architecture tests: detection/application layer import boundaries."""

from __future__ import annotations

from pathlib import Path

from tests.architecture.import_boundaries import collect_import_violations

DOMAIN_DIR = Path("rescue_ai/domain")
FORBIDDEN_PREFIX = "rescue_ai.infrastructure"


def test_domain_does_not_import_infrastructure() -> None:
    violations = collect_import_violations(
        target_dir=DOMAIN_DIR,
        forbidden_prefix=FORBIDDEN_PREFIX,
    )
    assert not violations


def test_domain_does_not_import_application() -> None:
    violations = collect_import_violations(
        target_dir=DOMAIN_DIR,
        forbidden_prefix="rescue_ai.application",
    )
    assert not violations
