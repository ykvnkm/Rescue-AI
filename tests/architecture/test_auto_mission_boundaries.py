"""Architecture tests for the automatic-mode application service.

Ensures ``AutoMissionService`` and the auto-mode ports stay inside the
intended layering: application depends on domain only; the new Postgres
auto-mode adapters live in infrastructure; no cross-layer leaks.
"""

from __future__ import annotations

from pathlib import Path

from tests.architecture.import_boundaries import collect_import_violations

AUTO_SERVICE = Path("rescue_ai/application/auto_mission_service.py")
POSTGRES_AUTO_REPOS = Path("rescue_ai/infrastructure/postgres_auto_repositories.py")


def test_auto_mission_service_does_not_import_infrastructure() -> None:
    violations = collect_import_violations(
        target_files=[AUTO_SERVICE],
        forbidden_prefix="rescue_ai.infrastructure",
    )
    assert not violations


def test_auto_mission_service_does_not_import_interfaces() -> None:
    violations = collect_import_violations(
        target_files=[AUTO_SERVICE],
        forbidden_prefix="rescue_ai.interfaces",
    )
    assert not violations


def test_postgres_auto_repositories_do_not_import_application() -> None:
    violations = collect_import_violations(
        target_files=[POSTGRES_AUTO_REPOS],
        forbidden_prefix="rescue_ai.application",
    )
    assert not violations


def test_postgres_auto_repositories_do_not_import_interfaces() -> None:
    violations = collect_import_violations(
        target_files=[POSTGRES_AUTO_REPOS],
        forbidden_prefix="rescue_ai.interfaces",
    )
    assert not violations
