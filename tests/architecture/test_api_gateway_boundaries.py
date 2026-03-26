"""Architecture boundary tests for API layer boundaries."""

from __future__ import annotations

from pathlib import Path

from tests.architecture.import_boundaries import collect_import_violations

API_DIR = Path("rescue_ai/interfaces/api")
FORBIDDEN_PREFIX = "rescue_ai.infrastructure"


def test_api_interfaces_have_no_direct_infrastructure_imports() -> None:
    violations = collect_import_violations(
        target_dir=API_DIR,
        forbidden_prefix=FORBIDDEN_PREFIX,
    )
    assert not violations
