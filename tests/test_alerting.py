"""The platform's alerting: its own alert rules (Argo CD) and Alertmanager's routing.

An alert that never fires looks exactly like a healthy system, so:
- every platform alert has a promtool scenario, and the scenarios pass;
- Alertmanager's config (deploy/kind/monitoring-values.yaml) is valid and routes each kind
  of alert to the right receiver (amtool).
An app's own alert rules are tested in the app's repo.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "deploy/argocd/platform/files/argocd-rules.yaml"
RUNBOOK = ROOT / "docs/RUNBOOK.md"
RUNBOOK_URL = "https://github.com/easypeasyops/platform/blob/main/docs/RUNBOOK.md"
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


def _anchor(heading: str) -> str:
    """GitHub's anchor for a Markdown heading: lower case, punctuation gone, spaces to -."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def test_every_runbook_link_names_a_heading() -> None:
    anchors = {
        _anchor(line.lstrip("#"))
        for line in RUNBOOK.read_text().splitlines()
        if line.startswith("#")
    }
    links = [
        rule["annotations"]["runbook_url"]
        for group in yaml.safe_load(RULES.read_text())["groups"]
        for rule in group["rules"]
        if "alert" in rule
    ]
    assert links, "no runbook links found"
    for link in links:
        assert link.startswith(RUNBOOK_URL + "#"), f"{link} is not in docs/RUNBOOK.md"
        assert link.split("#", 1)[1] in anchors, f"{link}: no such heading"


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
    # Where the stack puts them, /etc/alertmanager/config/, is /c/ in the amtool container.
    for name, text in values["alertmanager"]["templateFiles"].items():
        (directory / name).write_text(text)
        (directory / name).chmod(0o644)
    config_values = values["alertmanager"]["config"]
    assert config_values["templates"] == ["/etc/alertmanager/config/*.tmpl"]
    config_values["templates"] = ["/c/*.tmpl"]
    config = directory / "alertmanager.yaml"
    config.write_text(yaml.safe_dump(config_values))
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
        # aipostit: every alert of its own to its environment's Slack channel.
        (["namespace=aipostit-prod", "severity=page"], "aipostit-prod"),
        (["namespace=aipostit-prod", "severity=ticket"], "aipostit-prod"),
        (["namespace=aipostit-staging", "severity=page"], "aipostit-staging"),
        (["namespace=aipostit-staging", "severity=ticket"], "aipostit-staging"),
        # The stack's alerts on aipostit's pods: its own rules cover them.
        (["namespace=aipostit-prod", "severity=warning"], "null"),
        # Argo CD on aipostit's Applications: by Application; one not staging's goes to prod.
        (["project=aipostit", "name=aipostit-staging", "severity=ticket"], "aipostit-staging"),
        (["project=aipostit", "name=aipostit-prod", "severity=ticket"], "aipostit-prod"),
        (["project=aipostit", "name=aipostit-preview", "severity=ticket"], "aipostit-prod"),
        (["project=shortly", "name=shortly-prod", "severity=ticket"], "tickets"),
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


def _slack(config: Path, data: dict[str, Any]) -> str:
    """aipostit's Slack title and text for this notification (template.Data)."""
    (config.parent / "data.json").write_text(json.dumps(data))
    (config.parent / "data.json").chmod(0o644)
    text = '{{ template "aipostit.slack.title" . }}\n{{ template "aipostit.slack.text" . }}'
    result = _amtool(
        config,
        "template", "render", "--template.glob=/c/*.tmpl", f"--template.text={text}",
        "--template.data=/c/data.json",
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def _notification(receiver: str, status: str, labels: dict[str, str]) -> dict[str, Any]:
    alert = {
        "status": status,
        "labels": {"alertname": "AipostitApiDown", **labels},
        "annotations": {"summary": "The API is down", "runbook_url": "https://example.test#x"},
    }
    return {
        "receiver": receiver,
        "status": status,
        "alerts": [alert],
        "groupLabels": {},
        "commonLabels": alert["labels"],
        "commonAnnotations": alert["annotations"],
        "externalURL": "http://alertmanager.localhost:8090",
    }


@pytest.mark.integration
def test_a_prod_page_is_red_and_notifies_the_channel(alertmanager_config: Path) -> None:
    labels = {"namespace": "aipostit-prod", "severity": "page"}
    message = _slack(alertmanager_config, _notification("aipostit-prod", "firing", labels))
    assert message.startswith("🔴 AipostitApiDown in prod\n<!channel> "), message
    assert "The API is down" in message
    assert "<https://example.test#x|Runbook>" in message


@pytest.mark.integration
@pytest.mark.parametrize(
    ("receiver", "status", "labels", "title"),
    [
        ("aipostit-prod", "firing", {"namespace": "aipostit-prod", "severity": "ticket"},
         "🟡 AipostitApiDown in prod"),
        ("aipostit-prod", "resolved", {"namespace": "aipostit-prod", "severity": "page"},
         "🔴 RESOLVED AipostitApiDown in prod"),
        ("aipostit-staging", "firing", {"namespace": "aipostit-staging", "severity": "page"},
         "🔴 AipostitApiDown in staging"),
        ("aipostit-prod", "firing", {"name": "aipostit-prod", "severity": "ticket"},
         "🟡 AipostitApiDown in prod"),
    ],
)  # fmt: skip
def test_other_messages_never_notify_the_channel(
    alertmanager_config: Path, receiver: str, status: str, labels: dict[str, str], title: str
) -> None:
    message = _slack(alertmanager_config, _notification(receiver, status, labels))
    assert message.startswith(title + "\n"), message
    assert "<!channel>" not in message
