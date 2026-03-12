from __future__ import annotations

from pathlib import Path

from tests.architecture.import_boundaries import collect_import_violations

TARGET_FILES = [
    Path("services/api_gateway/presentation/http/routes.py"),
    Path("services/api_gateway/dependencies.py"),
]
FORBIDDEN_PREFIX = "services.detection_service"


def test_api_gateway_no_direct_detection_imports() -> None:
    violations = collect_import_violations(
        target_files=TARGET_FILES,
        forbidden_prefix=FORBIDDEN_PREFIX,
    )
    assert not violations
