"""The platform's alerting: its own alert rules (Argo CD) and Alertmanager's routing.

An alert that never fires looks exactly like a healthy system, so:
- every platform alert has a promtool scenario, and the scenarios pass;
- Alertmanager's config (deploy/kind/monitoring-values.yaml) is valid and routes each kind
  of alert to the right receiver (amtool).
An app's own alert rules are tested in the app's repo.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "deploy/argocd/platform/files/argocd-rules.yaml"
PROMTOOL_TESTS = ROOT / "deploy/prometheus/tests"
PROMETHEUS_IMAGE = "prom/prometheus:v3.15.0"
# The Alertmanager that kube-prometheus-stack (deploy/argocd/platform/values.yaml) runs.
ALERTMANAGER_IMAGE = "quay.io/prometheus/alertmanager:v0.34.1"


def _docker() -> None:
    if (
        shutil.which("docker")
        and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    ):
        return
    if os.environ.get("CI"):
        pytest.fail("Docker is required for the alerting tests in CI")
    pytest.skip("Docker not available")


def test_every_platform_alert_has_a_scenario() -> None:
    alerts = {
        rule["alert"]
        for group in yaml.safe_load(RULES.read_text())["groups"]
        for rule in group["rules"]
        if "alert" in rule
    }
    tested = {
        case["alertname"]
        for path in PROMTOOL_TESTS.glob("*.yaml")
        for test in yaml.safe_load(path.read_text())["tests"]
        for case in test.get("alert_rule_test", [])
        if case["exp_alerts"]  # proven to fire, not only to stay quiet
    }
    assert alerts, "no alerts found"
    assert alerts <= tested, f"alerts never shown to fire: {sorted(alerts - tested)}"


@pytest.mark.integration
def test_promtool_scenarios_pass() -> None:
    _docker()
    result = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{ROOT / 'deploy'}:/d:ro", "--entrypoint", "promtool",
         PROMETHEUS_IMAGE, "test", "rules",
         *[f"/d/prometheus/tests/{p.name}" for p in sorted(PROMTOOL_TESTS.glob("*.yaml"))]],
        capture_output=True,
        text=True,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture(scope="module")
def alertmanager_config(tmp_path_factory: pytest.TempPathFactory) -> Path:
    _docker()
    values = yaml.safe_load((ROOT / "deploy/kind/monitoring-values.yaml").read_text())
    directory = tmp_path_factory.mktemp("alertmanager")
    directory.chmod(0o755)  # the amtool container runs as nobody
    config = directory / "alertmanager.yaml"
    config.write_text(yaml.safe_dump(values["alertmanager"]["config"]))
    config.chmod(0o644)
    return config


def _amtool(config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "run", "--rm", "-v", f"{config.parent}:/c:ro", "--entrypoint", "amtool",
         ALERTMANAGER_IMAGE, *args],
        capture_output=True,
        text=True,
    )  # fmt: skip


@pytest.mark.integration
def test_alertmanager_config_is_valid(alertmanager_config: Path) -> None:
    result = _amtool(alertmanager_config, "check-config", f"/c/{alertmanager_config.name}")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.integration
@pytest.mark.parametrize(
    ("labels", "receiver"),
    [
        (["namespace=prod", "severity=page"], "pager"),
        (["namespace=prod", "severity=ticket"], "tickets"),
        # Any app's staging never pages: <app>-staging (platform plan, Phase 2).
        (["namespace=shortly-staging", "severity=page"], "staging"),
        (["namespace=shortly-prod", "severity=page"], "pager"),
        (["namespace=prodstaging", "severity=page"], "pager"),  # the regex is anchored
        (["alertname=Watchdog", "severity=none"], "null"),
        (["namespace=monitoring", "severity=warning"], "null"),  # the stack's own alerts
    ],
)
def test_alerts_route_to_the_right_receiver(
    alertmanager_config: Path, labels: list[str], receiver: str
) -> None:
    result = _amtool(
        alertmanager_config,
        "config", "routes", "test", f"--config.file=/c/{alertmanager_config.name}",
        f"--verify.receivers={receiver}", *labels,
    )  # fmt: skip
    assert result.returncode == 0, f"{labels} -> {result.stdout.strip()}, want {receiver}"
