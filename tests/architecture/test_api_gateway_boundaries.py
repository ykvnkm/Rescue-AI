from __future__ import annotations

import ast
from pathlib import Path

# pylint: disable=duplicate-code


TARGET_FILES = [
    Path("services/api_gateway/presentation/http/routes.py"),
    Path("services/api_gateway/dependencies.py"),
]
FORBIDDEN_PREFIX = "services.detection_service"


def _violations() -> list[tuple[Path, str]]:
    violations: list[tuple[Path, str]] = []
    for file_path in TARGET_FILES:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(FORBIDDEN_PREFIX):
                        violations.append((file_path, alias.name))
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith(FORBIDDEN_PREFIX):
                    violations.append((file_path, module))
    return violations


def test_api_gateway_no_direct_detection_imports() -> None:
    assert not _violations()
