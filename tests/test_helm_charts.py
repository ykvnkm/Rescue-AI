"""Smoke-тесты для Helm-чартов P3 / ADR-0008.

Не требуют живого кластера — только установленный helm CLI.
Если helm нет на машине — все тесты skip'аются (на CI helm есть).

Что проверяем:
  - umbrella + два дочерних чарта проходят `helm lint` без ошибок;
  - каждый из 4 values-файлов рендерится корректно;
  - sync-worker появляется ТОЛЬКО в hybrid;
  - vault-аннотации появляются в cloud/offline/hybrid (где
    secrets.source = vault), и НЕ появляются в dev.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHARTS_DIR = REPO_ROOT / "infra" / "k8s" / "charts"
VALUES_DIR = REPO_ROOT / "infra" / "k8s" / "values"
UMBRELLA = CHARTS_DIR / "rescue-ai"


def _helm_available() -> bool:
    return shutil.which("helm") is not None


pytestmark = pytest.mark.skipif(
    not _helm_available(),
    reason="helm CLI not installed (skipped on dev machines without helm)",
)


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )


@pytest.fixture(scope="module", autouse=True)
def _ensure_dependencies() -> None:
    """Подтянуть subchart'ы перед template/lint."""
    result = _run(["helm", "dependency", "update", str(UMBRELLA)])
    if result.returncode != 0:
        pytest.skip(
            "helm dependency update failed (no internet access?): "
            f"{result.stderr}"
        )


@pytest.mark.parametrize(
    "chart",
    [
        "rescue-ai-api",
        "rescue-ai-sync-worker",
        "rescue-ai",
    ],
)
def test_helm_lint(chart: str) -> None:
    result = _run(["helm", "lint", str(CHARTS_DIR / chart)])
    assert result.returncode == 0, (
        f"helm lint for {chart} failed:\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.parametrize(
    "profile",
    ["dev", "cloud", "offline", "hybrid"],
)
def test_helm_template_renders(profile: str) -> None:
    values = VALUES_DIR / f"{profile}.yaml"
    result = _run(
        ["helm", "template", "rescue-ai", str(UMBRELLA), "-f", str(values)]
    )
    assert result.returncode == 0, (
        f"helm template for {profile} failed:\n{result.stderr}"
    )
    assert result.stdout.strip(), "rendered template is empty"


def test_sync_worker_only_in_hybrid() -> None:
    rendered: dict[str, str] = {}
    for profile in ("dev", "cloud", "offline", "hybrid"):
        values = VALUES_DIR / f"{profile}.yaml"
        result = _run(
            ["helm", "template", "rescue-ai", str(UMBRELLA), "-f", str(values)]
        )
        assert result.returncode == 0
        rendered[profile] = result.stdout

    # sync-worker — это hybrid-only компонент.
    assert "rescue-ai-sync-worker" in rendered["hybrid"]
    for profile in ("dev", "cloud", "offline"):
        assert "kind: Deployment" not in (
            "\n".join(
                line
                for line in rendered[profile].splitlines()
                if "rescue-ai-sync-worker" in line or "kind: Deployment" in line
            )
            if "rescue-ai-sync-worker" in rendered[profile]
            else "no-deployment"
        ), f"unexpected sync-worker Deployment in {profile}"


def test_vault_annotations_match_secrets_source() -> None:
    cases = [
        ("dev", False),
        ("cloud", True),
        ("offline", True),
        ("hybrid", True),
    ]
    for profile, expect_vault in cases:
        values = VALUES_DIR / f"{profile}.yaml"
        result = _run(
            ["helm", "template", "rescue-ai", str(UMBRELLA), "-f", str(values)]
        )
        assert result.returncode == 0
        has_annotation = "vault.hashicorp.com/agent-inject" in result.stdout
        assert has_annotation is expect_vault, (
            f"profile={profile}: vault.hashicorp.com/agent-inject "
            f"expected={expect_vault}, got={has_annotation}"
        )
