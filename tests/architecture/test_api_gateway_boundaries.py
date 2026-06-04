"""Architecture boundary tests for API layer boundaries."""

from __future__ import annotations

from pathlib import Path

from tests.architecture.import_boundaries import collect_import_violations

API_DIR = Path("rescue_ai/interfaces/api")
FORBIDDEN_PREFIX = "rescue_ai.infrastructure"
ALLOWED_COMPOSITION_IMPORTS = {
    (
        Path("rescue_ai/interfaces/api/routes_auto_sessions.py"),
        "rescue_ai.infrastructure.artifact_storage",
    ),
    (
        Path("rescue_ai/interfaces/api/routes_auto_sessions.py"),
        "rescue_ai.infrastructure.s3_mission_source",
    ),
}


def test_api_interfaces_have_no_direct_infrastructure_imports() -> None:
    violations = collect_import_violations(
        target_dir=API_DIR,
        forbidden_prefix=FORBIDDEN_PREFIX,
    )
    violations = [
        violation
        for violation in violations
        if violation not in ALLOWED_COMPOSITION_IMPORTS
    ]
    assert not violations
