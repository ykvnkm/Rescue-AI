"""Architecture tests: ``rescue_ai/infrastructure/detectors/`` import boundaries.

Detector wrappers may depend on domain (for :class:`Detection`) and on
application (:class:`InferenceConfig`), but must not reach up into the
interfaces layer.  The vendored ``nanodet_core`` subpackage must remain
self-contained — no ``rescue_ai.*`` imports outside its own subtree.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture.import_boundaries import collect_import_violations

DETECTORS_DIR = Path("rescue_ai/infrastructure/detectors")
DETECTOR_WRAPPERS = [
    DETECTORS_DIR / "__init__.py",
    DETECTORS_DIR / "yolo_detector.py",
    DETECTORS_DIR / "nanodet_detector.py",
    DETECTORS_DIR / "factory.py",
]
NANODET_CORE_DIR = DETECTORS_DIR / "nanodet_core"
NANODET_CORE_PREFIX = "rescue_ai.infrastructure.detectors.nanodet_core"


def test_detector_wrappers_do_not_import_interfaces() -> None:
    violations = collect_import_violations(
        target_files=DETECTOR_WRAPPERS,
        forbidden_prefix="rescue_ai.interfaces",
    )
    assert not violations


def test_nanodet_core_does_not_escape_its_subpackage() -> None:
    offenders: list[tuple[Path, str]] = []
    for path in NANODET_CORE_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(
                        "rescue_ai."
                    ) and not alias.name.startswith(NANODET_CORE_PREFIX):
                        offenders.append((path, alias.name))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith("rescue_ai.") and not module.startswith(
                    NANODET_CORE_PREFIX
                ):
                    offenders.append((path, module))
    assert not offenders, offenders
