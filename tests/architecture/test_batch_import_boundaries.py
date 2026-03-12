from __future__ import annotations

import ast
from pathlib import Path

APPLICATION_DIR = Path("libs/batch/application")
FORBIDDEN_PREFIX = "libs.batch.infrastructure"


def _violations() -> list[tuple[Path, str]]:
    violations: list[tuple[Path, str]] = []
    for file_path in APPLICATION_DIR.glob("*.py"):
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


def test_batch_application_does_not_import_infrastructure() -> None:
    assert not _violations()
