"""Architecture boundary test: interfaces layer must not leak infra into routes."""

from __future__ import annotations

from pathlib import Path

from tests.architecture.import_boundaries import collect_import_violations

TARGET_FILES = [
    Path("rescue_ai/interfaces/api/routes.py"),
    Path("rescue_ai/interfaces/api/dependencies.py"),
]
FORBIDDEN_PREFIX = "rescue_ai.infrastructure"


def test_interfaces_no_direct_infrastructure_imports() -> None:
    violations = collect_import_violations(
        target_files=TARGET_FILES,
        forbidden_prefix=FORBIDDEN_PREFIX,
    )
    # Interfaces layer may import infrastructure for DI wiring in dependencies.py.
    # Only routes.py must be free of infrastructure imports.
    route_violations = [v for v in violations if v[0].name == "routes.py"]
    assert not route_violations
