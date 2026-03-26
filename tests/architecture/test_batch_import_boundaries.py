from __future__ import annotations

from pathlib import Path

from tests.architecture.import_boundaries import collect_import_violations

APPLICATION_DIR = Path("rescue_ai/application")
FORBIDDEN_PREFIX = "rescue_ai.infrastructure"


def test_application_does_not_import_infrastructure() -> None:
    violations = collect_import_violations(
        target_dir=APPLICATION_DIR,
        forbidden_prefix=FORBIDDEN_PREFIX,
    )
    assert not violations
