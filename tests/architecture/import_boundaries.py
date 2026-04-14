"""Utility for verifying import boundary rules between layers."""

from __future__ import annotations

import ast
from pathlib import Path


def collect_import_violations(
    *,
    target_files: list[Path] | None = None,
    target_dir: Path | None = None,
    forbidden_prefix: str,
) -> list[tuple[Path, str]]:
    if target_files is not None:
        files = target_files
    elif target_dir is not None:
        files = list(target_dir.glob("*.py"))
    else:
        raise ValueError("Either target_files or target_dir must be provided")

    violations: list[tuple[Path, str]] = []
    for file_path in files:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(forbidden_prefix):
                        violations.append((file_path, alias.name))
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith(forbidden_prefix):
                    violations.append((file_path, module))
    return violations
