"""Smoke-тесты для Helm-чартов P3 / ADR-0008.

Не требуют живого кластера — только установленный helm CLI.
Если helm нет на машине — все тесты skip'аются (на CI helm есть).

Что проверяем:
  - umbrella + дочерние чарты проходят `helm lint` без ошибок;
  - каждый из 3 values-файлов рендерится корректно;
  - sync-worker появляется ТОЛЬКО в offline;
  - vault-аннотации появляются в offline/cloud (где
    secrets.source = vault) и НЕ появляются в dev.
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
    repos = [
        ("bitnami", "https://charts.bitnami.com/bitnami"),
        ("hashicorp", "https://helm.releases.hashicorp.com"),
        ("apache-airflow", "https://airflow.apache.org"),
    ]
    for name, url in repos:
        result = _run(["helm", "repo", "add", name, url, "--force-update"])
        if result.returncode != 0 and "already exists" not in result.stderr:
            pytest.skip(f"helm repo add {name} failed: {result.stderr}")

    result = _run(["helm", "repo", "update"])
    if result.returncode != 0:
        pytest.skip(f"helm repo update failed (no internet access?): {result.stderr}")

    result = _run(["helm", "dependency", "update", str(UMBRELLA)])
    if result.returncode != 0:
        pytest.skip(
            "helm dependency update failed (no internet access?): " f"{result.stderr}"
        )


@pytest.mark.parametrize(
    "chart",
    [
        "rescue-ai-api",
        "rescue-ai-detection",
        "rescue-ai-nav-engine",
        "rescue-ai-sync-worker",
        "rescue-ai-batch-exporter",
        "rescue-ai",
    ],
)
def test_helm_lint(chart: str) -> None:
    result = _run(["helm", "lint", str(CHARTS_DIR / chart)])
    assert (
        result.returncode == 0
    ), f"helm lint for {chart} failed:\n{result.stdout}\n{result.stderr}"


@pytest.mark.parametrize(
    "profile",
    ["dev", "cloud", "offline"],
)
def test_helm_template_renders(profile: str) -> None:
    values = VALUES_DIR / f"{profile}.yaml"
    result = _run(["helm", "template", "rescue-ai", str(UMBRELLA), "-f", str(values)])
    assert (
        result.returncode == 0
    ), f"helm template for {profile} failed:\n{result.stderr}"
    assert result.stdout.strip(), "rendered template is empty"


def test_sync_worker_only_in_offline() -> None:
    """sync-worker — это offline-only компонент.

    В offline-профиле sync-worker драйнит outbox в центральный контур
    при наличии связи. В cloud-профиле принимающая сторона, поэтому
    sync-worker отсутствует. В dev-профиле базовый smoke без репликации.
    """
    rendered: dict[str, str] = {}
    for profile in ("dev", "cloud", "offline"):
        values = VALUES_DIR / f"{profile}.yaml"
        result = _run(
            ["helm", "template", "rescue-ai", str(UMBRELLA), "-f", str(values)]
        )
        assert result.returncode == 0
        rendered[profile] = result.stdout

    assert "rescue-ai-sync-worker" in rendered["offline"]
    for profile in ("dev", "cloud"):
        assert (
            "rescue-ai-sync-worker" not in rendered[profile]
        ), f"unexpected sync-worker in {profile}"


def test_batch_exporter_only_in_cloud() -> None:
    """batch-exporter живёт только в центральном кластере.

    Ежедневный batch-пересчёт качества модели имеет смысл только
    над аккумулированным набором миссий со всех станций — он
    выполняется централизованно. На станции batch-сервис не нужен.
    В dev-профиле (smoke) тоже не разворачивается.
    """
    rendered: dict[str, str] = {}
    for profile in ("dev", "cloud", "offline"):
        values = VALUES_DIR / f"{profile}.yaml"
        result = _run(
            ["helm", "template", "rescue-ai", str(UMBRELLA), "-f", str(values)]
        )
        assert result.returncode == 0
        rendered[profile] = result.stdout

    assert "rescue-ai-batch-exporter" in rendered["cloud"]
    assert "rescue-ai-batch-exporter" not in rendered["offline"]
    assert "rescue-ai-batch-exporter" not in rendered["dev"]


def test_detection_and_nav_engine_in_both_workload_profiles() -> None:
    """detection и nav-engine присутствуют там, где идут миссии.

    Обе подсистемы обрабатывают живые кадры, поэтому включены
    и в offline (полевая станция / VPS), и в cloud (стендовые,
    потоковые, демонстрационные миссии).
    """
    rendered: dict[str, str] = {}
    for profile in ("offline", "cloud"):
        values = VALUES_DIR / f"{profile}.yaml"
        result = _run(
            ["helm", "template", "rescue-ai", str(UMBRELLA), "-f", str(values)]
        )
        assert result.returncode == 0
        rendered[profile] = result.stdout

    for profile in ("offline", "cloud"):
        assert (
            "rescue-ai-rescue-ai-detection" in rendered[profile]
        ), f"rescue-ai-detection missing in {profile}"
        assert (
            "rescue-ai-rescue-ai-nav-engine" in rendered[profile]
        ), f"rescue-ai-nav-engine missing in {profile}"


def test_vault_annotations_match_secrets_source() -> None:
    cases = [
        ("dev", False),
        ("cloud", True),
        ("offline", True),
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
