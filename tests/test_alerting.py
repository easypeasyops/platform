"""The platform's alerting: its own alert rules (Argo CD, Prometheus, the logs, the front
door, External Secrets, cert-manager, Kyverno, CloudNativePG, Argo Rollouts) and
Alertmanager's routing.

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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
RULES = sorted((ROOT / "deploy/argocd/platform/files").glob("*-rules.yaml"))
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
        for path in RULES
        for group in yaml.safe_load(path.read_text())["groups"]
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
        for path in RULES
        for group in yaml.safe_load(path.read_text())["groups"]
        for rule in group["rules"]
        if "alert" in rule
    ]
    assert links, "no runbook links found"
    for link in links:
        assert link.startswith(RUNBOOK_URL + "#"), f"{link} is not in docs/RUNBOOK.md"
        assert link.split("#", 1)[1] in anchors, f"{link}: no such heading"


def test_the_buffer_alert_knows_the_collectors_cap() -> None:
    """The collector has no metric for its buffer's cap, so the rule spells it out."""
    values = yaml.safe_load((ROOT / "deploy/kind/log-collector-values.yaml").read_text())
    assert [url["maxDiskUsagePerURL"] for url in values["remoteWrite"]] == ["1GiB"]
    rules = yaml.safe_load((ROOT / "deploy/argocd/platform/files/logs-rules.yaml").read_text())
    (rule,) = [
        rule
        for group in rules["groups"]
        for rule in group["rules"]
        if rule["alert"] == "LogCollectorBufferNearlyFull"
    ]
    assert rule["expr"].endswith(f"> 0.8 * {2**30}")
    assert "at 1 GiB" in rule["annotations"]["description"]


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
        # The stack's own alerts: warning and critical on the platform's namespaces, or on
        # none, to the platform's channel; info, and any on an app's namespace, to nobody.
        (["namespace=monitoring", "severity=warning"], "platform"),
        (["namespace=kube-system", "severity=critical"], "platform"),
        (["alertname=KubeAPIDown", "severity=critical"], "platform"),
        # The log pipeline's (files/logs-rules.yaml), by the namespace each carries.
        (["alertname=LogsNotArriving", "namespace=monitoring", "severity=warning"], "platform"),
        (["namespace=log-collector", "severity=critical"], "platform"),
        (["namespace=monitoring", "severity=info"], "null"),
        (["alertname=InfoInhibitor", "severity=none"], "null"),
        (["namespace=aipostit-prod", "severity=critical"], "null"),
        (["namespace=shortly-prod", "severity=warning"], "null"),
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
        # The front door's, whichever app's route: the platform's. The route's namespace is
        # in route_namespace, not namespace, so aipostit's routes above don't take it.
        (
            [
                "alertname=FrontDoorRouteWithoutBackend",
                "route_namespace=aipostit-staging",
                "route=aipostit",
                "severity=ticket",
            ],
            "platform",
        ),
        # The front door down: the proxies (critical) and the controller, no route_namespace.
        (
            ["alertname=FrontDoorDown", "namespace=envoy-gateway-system", "severity=critical"],
            "platform",
        ),
        (
            [
                "alertname=FrontDoorControllerDown",
                "namespace=envoy-gateway-system",
                "severity=warning",
            ],
            "platform",
        ),
        # Argo Rollouts' on an app's canary: the platform's, not aipostit's channel. The
        # Rollout's namespace is in exported_namespace.
        (
            [
                "alertname=CanaryAnalysisErrored",
                "namespace=argo-rollouts",
                "exported_namespace=aipostit-prod",
                "severity=warning",
            ],
            "platform",
        ),
        # The platform's own: Argo CD down (no project) and on the platform's Applications.
        (["alertname=ArgoCDDown", "severity=ticket"], "platform"),
        (
            [
                "alertname=ArgoCDAppUnhealthy",
                "project=platform",
                "name=monitoring",
                "severity=ticket",
            ],
            "platform",
        ),
        (
            ["alertname=ArgoCDSyncFailed", "project=apps", "name=apps", "severity=ticket"],
            "platform",
        ),
        (
            [
                "alertname=ArgoCDSyncFailed",
                "project=shortly",
                "name=shortly-prod",
                "severity=ticket",
            ],
            "tickets",
        ),
        # deploybot: Argo CD on its Application to aipostit's prod channel; the stack's
        # alerts on its pod stay on "null", as aipostit's.
        (["project=deploybot", "name=deploybot-ops", "severity=ticket"], "aipostit-prod"),
        (["namespace=deploybot-ops", "severity=warning"], "null"),
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


def test_the_stack_route_names_every_platform_namespace() -> None:
    """A namespace the platform chart deploys to, missing from the route, alerts nobody."""
    values = yaml.safe_load((ROOT / "deploy/kind/monitoring-values.yaml").read_text())
    (matcher,) = [
        matcher
        for route in values["alertmanager"]["config"]["route"]["routes"]
        for matcher in route["matchers"]
        if route["receiver"] == "platform" and matcher.startswith("namespace")
    ]
    routed = set(matcher.split('"')[1].split("|"))
    deployed = {
        name
        for path in (ROOT / "deploy/argocd/platform").rglob("*.yaml")
        for name in re.findall(r"^\s*namespace: ([a-z0-9-]+)$", path.read_text(), re.M)
    }
    assert deployed, "no namespaces found"
    assert deployed <= routed, f"not in the platform's route: {sorted(deployed - routed)}"


def _slack(config: Path, data: dict[str, Any], who: str = "aipostit") -> str:
    """aipostit's (or the platform's) Slack title and text for this notification."""
    (config.parent / "data.json").write_text(json.dumps(data))
    (config.parent / "data.json").chmod(0o644)
    text = f'{{{{ template "{who}.slack.title" . }}}}\n{{{{ template "{who}.slack.text" . }}}}'
    result = _amtool(
        config,
        "template", "render", "--template.glob=/c/*.tmpl", f"--template.text={text}",
        "--template.data=/c/data.json",
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


FIRED = datetime(2026, 11, 14, 3, 4, 5, tzinfo=UTC)
QUERY = "http://prometheus.localhost:8090/graph?g0.expr=up&g0.tab=1"


def _notification(
    receiver: str,
    status: str,
    labels: dict[str, str],
    description: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, Any]:
    annotations = {"summary": "The API is down", "runbook_url": "https://example.test#x"}
    if description:
        annotations["description"] = description
    alert = {
        "status": status,
        "labels": {"alertname": "AipostitApiDown", **labels},
        "annotations": {**annotations, **(extra or {})},
        "startsAt": FIRED.isoformat(),
        "generatorURL": QUERY,
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
        ("aipostit-prod", "firing", {"name": "deploybot-ops", "severity": "ticket"},
         "🟡 AipostitApiDown in deploybot-ops"),
    ],
)  # fmt: skip
def test_other_messages_never_notify_the_channel(
    alertmanager_config: Path, receiver: str, status: str, labels: dict[str, str], title: str
) -> None:
    message = _slack(alertmanager_config, _notification(receiver, status, labels))
    assert message.startswith(title + "\n"), message
    assert "<!channel>" not in message


@pytest.mark.integration
@pytest.mark.parametrize(
    ("labels", "title"),
    [
        ({"alertname": "ArgoCDAppUnhealthy", "name": "monitoring", "project": "platform"},
         "🟡 ArgoCDAppUnhealthy on monitoring"),
        ({"alertname": "ArgoCDDown"}, "🟡 ArgoCDDown"),
        # The front door's: the route is in the summary (front_door_test.yaml), below.
        ({"alertname": "FrontDoorRouteWithoutBackend", "route_namespace": "aipostit-staging",
          "route": "aipostit"}, "🟡 FrontDoorRouteWithoutBackend"),
    ],
)  # fmt: skip
def test_a_platform_message_names_the_alert_and_its_application(
    alertmanager_config: Path, labels: dict[str, str], title: str
) -> None:
    data = _notification("platform", "firing", {**labels, "severity": "ticket"})
    message = _slack(alertmanager_config, data, "platform")
    assert message.startswith(title + "\n"), message
    assert "• The API is down" in message  # a ticket's summary, which names what it is about
    assert "<!channel>" not in message
    assert "<https://example.test#x|Runbook>" in message


@pytest.mark.integration
@pytest.mark.parametrize(
    ("labels", "title"),
    [
        ({"alertname": "KubePodCrashLooping", "namespace": "monitoring", "severity": "warning"},
         "🟡 KubePodCrashLooping in monitoring"),
        ({"alertname": "KubeAPIDown", "severity": "critical"}, "🔴 KubeAPIDown"),
    ],
)  # fmt: skip
def test_a_stack_message_says_where_and_gives_the_description(
    alertmanager_config: Path, labels: dict[str, str], title: str
) -> None:
    data = _notification("platform", "firing", labels, "Pod monitoring/grafana-0 is waiting")
    message = _slack(alertmanager_config, data, "platform")
    assert message.startswith(title + "\n"), message
    assert "• Pod monitoring/grafana-0 is waiting" in message
    assert "The API is down" not in message  # the summary alone doesn't say which pod
    assert "<!channel>" not in message


@pytest.mark.integration
def test_an_argo_cd_message_keeps_its_summary(alertmanager_config: Path) -> None:
    labels = {"alertname": "ArgoCDAppUnhealthy", "name": "monitoring", "severity": "ticket"}
    data = _notification("platform", "firing", labels, "Argo CD reports it as not working")
    message = _slack(alertmanager_config, data, "platform")
    assert "• The API is down" in message
    assert "Argo CD reports" not in message


def _links(message: str) -> dict[str, str]:
    """The message's links, by their text."""
    return {text: url for url, text in re.findall(r"<([^|>]+)\|([^>]+)>", message)}


def _ms(at: datetime) -> str:
    return str(int(at.timestamp() * 1000))


@pytest.mark.integration
@pytest.mark.parametrize(
    ("who", "labels", "extra", "namespace", "words", "dashboard"),
    [
        # An alert with a namespace: that namespace's logs.
        ("aipostit", {"namespace": "aipostit-prod", "severity": "page"}, {},
         "aipostit-prod", "", None),
        # The Certificate's or the ExternalSecret's own namespace, not the exporter's.
        ("platform", {"alertname": "ExternalSecretNotSynced", "namespace": "external-secrets",
                      "exported_namespace": "backups", "severity": "warning"}, {},
         "backups", "", None),
        # An Application's name only (Argo CD's): Argo CD's lines that name it.
        ("aipostit", {"alertname": "ArgoCDSyncFailed", "project": "aipostit",
                      "name": "aipostit-staging", "severity": "ticket"}, {},
         "argocd", ' "aipostit-staging"', None),
        # The front door's: the route's namespace, and the rule's dashboard.
        ("platform", {"alertname": "FrontDoorRouteWithoutBackend",
                      "route_namespace": "aipostit-staging", "route": "aipostit",
                      "severity": "ticket"}, {"dashboard_url": "/d/front-door"},
         "aipostit-staging", "", "/d/front-door"),
    ],
)  # fmt: skip
def test_a_firing_message_links_to_the_evidence(
    alertmanager_config: Path,
    who: str,
    labels: dict[str, str],
    extra: dict[str, str],
    namespace: str,
    words: str,
    dashboard: str | None,
) -> None:
    data = _notification(f"{who}-x", "firing", labels, extra=extra)
    links = _links(_slack(alertmanager_config, data, who))
    expected = {"Runbook", "Query", "Logs", "Silence"} | ({"Dashboard"} if dashboard else set())
    assert set(links) == expected, links

    # The expression, at the moment it fired.
    assert links["Query"] == QUERY + "&g0.moment_input=2026-11-14+03%3A04%3A05"

    # The namespace's logs, from an hour before it fired to half an hour after.
    logs = urlsplit(links["Logs"])
    assert f"{logs.scheme}://{logs.netloc}{logs.path}" == "http://grafana.localhost:8090/explore"
    (pane,) = json.loads(parse_qs(logs.query)["panes"][0]).values()
    assert pane["datasource"] == "victorialogs"
    (query,) = pane["queries"]
    assert query["datasource"] == {"type": "victoriametrics-logs-datasource",
                                   "uid": "victorialogs"}  # fmt: skip
    assert query["expr"] == f'{{kubernetes.pod_namespace="{namespace}"}}{words}'
    window = {"from": _ms(FIRED - timedelta(hours=1)), "to": _ms(FIRED + timedelta(minutes=30))}
    assert pane["range"] == window

    if dashboard:
        assert links["Dashboard"] == (
            f"http://grafana.localhost:8090{dashboard}?from={window['from']}&to={window['to']}"
        )

    # A new silence, filled in with the alert's labels.
    base, _, fragment = links["Silence"].partition("#")
    assert base == "http://alertmanager.localhost:8090/"
    path, _, query_string = fragment.partition("?")
    assert path == "/silences/new"
    matchers = parse_qs(query_string)["filter"][0]
    assert matchers[0] + matchers[-1] == "{}", matchers
    want = {"alertname": data["alerts"][0]["labels"]["alertname"], **labels}
    assert set(matchers[1:-1].split(",")) == {f'{k}="{v}"' for k, v in want.items()}


@pytest.mark.integration
@pytest.mark.parametrize("who", ["aipostit", "platform"])
def test_a_resolved_message_keeps_only_the_runbook(alertmanager_config: Path, who: str) -> None:
    labels = {"namespace": "aipostit-prod", "severity": "page"}
    data = _notification(f"{who}-x", "resolved", labels, extra={"dashboard_url": "/d/x"})
    assert set(_links(_slack(alertmanager_config, data, who))) == {"Runbook"}


def test_the_links_use_grafanas_address() -> None:
    values = yaml.safe_load((ROOT / "deploy/kind/monitoring-values.yaml").read_text())
    root_url = values["grafana"]["grafana.ini"]["server"]["root_url"]
    links = values["alertmanager"]["templateFiles"]["links-slack.tmpl"]
    assert f'{{{{ define "slack.grafana" }}}}{root_url}{{{{ end }}}}' in links
