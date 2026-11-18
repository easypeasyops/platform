"""The Overview's platform components, over one recording of the laptop cluster
(tests/fixtures/components.json, 2026-11-16 20:14 PST: every monitor, its targets, the
alerts, and Prometheus's answer to each query of app/components.toml), changed one thing
per test. Nothing was down; three alerts fired, one of them a Prometheus stack's."""

import html
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import components, cronjobs, history, monitors, status
from app.containers import HOUR, MEMORY, OOM, RESTARTS, UPTIME
from app.main import app
from tests import test_status

FIXTURES = Path(__file__).parent / "fixtures"
PATHS = {
    "/api/v1/namespaces": "namespaces",
    "/apis/monitoring.coreos.com/v1/servicemonitors": "servicemonitors",
    "/apis/monitoring.coreos.com/v1/podmonitors": "podmonitors",
    "/apis/monitoring.coreos.com/v1/probes": "probes",
    "/api/v1/targets": "targets",
    "/api/v1/alerts": "alerts",
}
NINE = [
    "Argo CD Healthy 3/3 up 0 of 27 Applications out of sync",
    "Front door Healthy 9/9 up Dashboard 7/7 hostnames answer through it",
    "Kyverno Healthy 3/3 up 2 images checked for provenance in 1 h, 0 refused or errored",
    "CloudNativePG Healthy 1/1 up no databases",
    "Argo Rollouts Healthy 1/1 up no Rollouts",
    "cert-manager Healthy 1/1 up 2/2 certificates ready",
    "External Secrets Healthy 3/3 up 6/6 ExternalSecrets synced",
    "Prometheus stack Alert 6/6 up Dashboard 0 rule evaluations failed in 15 min"
    " PrometheusDuplicateTimestamps",
    "Grafana Healthy 1/1 up Dashboard 25 dashboards",
]


class Cluster:
    """Answers both APIs from the recording; a query by its PromQL. A test changes
    `bodies` (a query's None: Prometheus refuses it), or names a path or a query in
    `down`."""

    def __init__(self) -> None:
        self.bodies: dict[str, Any] = json.loads((FIXTURES / "components.json").read_text())
        # The pods ready aren't in this recording: tests/test_replicas.py's, by query.
        replicas = json.loads((FIXTURES / "replicas.json").read_text())
        self.bodies["queries"] |= {q: replicas[key] for key, q in status.REPLICAS.items()}
        # Nor the containers' (tests/test_containers.py): no series, nothing to say.
        self.bodies["queries"] |= dict.fromkeys(
            (UPTIME, RESTARTS.format(HOUR), OOM, MEMORY), test_status.NO_SERIES
        )
        self.bodies["queries"] |= dict.fromkeys(
            cronjobs.QUERIES.values(), test_status.NO_SERIES
        )  # nor any CronJob
        self.down: set[str] = set()
        self.queries: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/query":
            query = request.url.params["query"]
            self.queries.append(query)
            if query in self.down:
                raise httpx.ConnectError("connection refused")
            if self.bodies["queries"][query] is None:  # Prometheus's answer to a bad query
                return httpx.Response(422, json={"status": "error", "error": "bad_data"})
            return httpx.Response(200, json=self.bodies["queries"][query])
        if request.url.path in self.down or request.url.path == "/api/v1/query_range":
            # The last 24 hours (app/history.py) aren't in this recording.
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json=self.bodies[PATHS[request.url.path]])

    def query(self, name: str, component: str) -> str:
        (found,) = (c["queries"][name] for c in components.COMPONENTS if c["name"] == component)
        return found  # type: ignore[no-any-return]


@pytest.fixture(autouse=True)
def cluster(monkeypatch: pytest.MonkeyPatch) -> Iterator[Cluster]:
    cluster = Cluster()
    monkeypatch.setattr(monitors, "transport", httpx.MockTransport(cluster))
    for module in (monitors, status, components, history):
        monkeypatch.setattr(module, "_cache", None)
    for module in (monitors, status):
        monkeypatch.setattr(module, "_last_good", {})
    yield cluster


def lines() -> list[str]:
    """Each component's line, as it reads: no tags."""
    response = TestClient(app).get("/")
    assert response.status_code == 200
    found = re.findall(r'<div class="component">(.*?)</div>', response.text, re.S)
    return [" ".join(html.unescape(re.sub(r"<.*?>", " ", line)).split()) for line in found]


def line(name: str) -> str:
    (found,) = (text for text in lines() if text.startswith(f"{name} "))
    return found


def test_a_line_per_component_from_the_recording() -> None:
    assert lines() == NINE


def test_a_firing_platform_alert_is_on_its_component_s_line(cluster: Cluster) -> None:
    """DatabaseBackupTooOld is in cnpg-system, where nothing is scraped: on no monitor's
    row, but on CloudNativePG's line, by its name."""
    alert = {
        "labels": {
            "alertname": "DatabaseBackupTooOld",
            "namespace": "cnpg-system",
            "exported_namespace": "aipostit-prod",
            "cluster": "aipostit-db",
            "severity": "warning",
        },
        "annotations": {"summary": "Database aipostit-prod/aipostit-db has no base backup"},
        "state": "firing",
        "activeAt": "2026-11-16T20:00:00Z",
    }
    cluster.bodies["alerts"]["data"]["alerts"].append(alert)
    assert lines() == [
        "CloudNativePG Alert 1/1 up no databases DatabaseBackupTooOld"
        if text.startswith("CloudNativePG ")
        else text
        for text in NINE
    ]
    page = TestClient(app).get("/").text
    href = "http://prometheus.localhost:8090/alerts?search=DatabaseBackupTooOld"
    assert f'<a class="alert" href="{href}">DatabaseBackupTooOld</a>' in page


def test_a_pending_alert_is_not_on_the_line(cluster: Cluster) -> None:
    for alert in cluster.bodies["alerts"]["data"]["alerts"]:
        alert["state"] = "pending"
    assert line("Prometheus stack") == (
        "Prometheus stack Healthy 6/6 up Dashboard 0 rule evaluations failed in 15 min"
    )


def test_a_monitor_that_does_not_exist_is_not_found_and_down(cluster: Cluster) -> None:
    items = cluster.bodies["servicemonitors"]["items"]
    items[:] = [i for i in items if i["metadata"]["name"] != "argocd-repo-server"]
    assert line("Argo CD") == (
        "Argo CD Down 2/2 up ServiceMonitor/argocd/argocd-repo-server : monitor not found"
        " 0 of 27 Applications out of sync"
    )


def test_a_target_down_turns_its_line_down(cluster: Cluster) -> None:
    targets = cluster.bodies["targets"]["data"]["activeTargets"]
    next(t for t in targets if t["labels"]["job"] == "grafana")["health"] = "down"
    assert line("Grafana") == "Grafana Down 0/1 up Dashboard 25 dashboards"


def test_a_failed_query_leaves_only_its_line_unknown_with_the_error(cluster: Cluster) -> None:
    cluster.down = {cluster.query("ready", "cert-manager")}
    assert line("cert-manager") == (
        "cert-manager Unknown 1/1 up signal unknown: ready: connection refused"
    )
    assert [t for t in lines() if not t.startswith("cert-manager ")] == [
        t for t in NINE if not t.startswith("cert-manager ")
    ]


def test_a_query_prometheus_refuses_is_unknown_with_its_answer(cluster: Cluster) -> None:
    cluster.bodies["queries"][cluster.query("dashboards", "Grafana")] = None
    assert line("Grafana") == (
        "Grafana Unknown 1/1 up Dashboard signal unknown: dashboards: Prometheus answered 422"
        ' {"status":"error","error":"bad_data"}'
    )


def test_a_query_with_more_than_one_number_is_unknown(cluster: Cluster) -> None:
    body = cluster.bodies["queries"][cluster.query("dashboards", "Grafana")]
    body["data"]["result"].append(body["data"]["result"][0])
    assert line("Grafana").startswith(
        "Grafana Unknown 1/1 up Dashboard signal unknown: dashboards: 2 series"
    )


def test_no_series_is_unknown_unless_there_is_nothing_to_measure(cluster: Cluster) -> None:
    cluster.bodies["queries"][cluster.query("applications", "Argo CD")]["data"]["result"] = []
    assert line("Argo CD") == "Argo CD Unknown 3/3 up signal unknown: no series for applications"
    assert line("CloudNativePG") == "CloudNativePG Healthy 1/1 up no databases"  # `empty`


def test_prometheus_never_answering_shows_no_line_healthy(cluster: Cluster) -> None:
    cluster.down = {"/api/v1/targets", "/api/v1/alerts"} | set(cluster.bodies["queries"])
    found = lines()
    assert len(found) == 9
    for text in found:
        assert re.fullmatch(
            r".+ Unknown targets unknown (Dashboard )?signal unknown: \w+: connection refused"
            r" alerts unknown",
            text,
        ), text


def test_the_queries_are_cached(cluster: Cluster) -> None:
    lines()
    asked = len(cluster.queries)
    lines()
    assert len(cluster.queries) == asked  # every query once, for monitors.TTL


def test_adding_a_component_to_the_config_adds_its_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cluster: Cluster
) -> None:
    config = tmp_path / "components.toml"
    config.write_text(
        components.CONFIG.read_text()
        + """
[[component]]
name = "Logs"
monitors = ["ServiceMonitor/monitoring/victoria-logs", "PodMonitor/log-collector/*"]
alerts = ["Log*"]
signal = "{streams:.0f} log streams"
[component.queries]
streams = 'sum(vl_streams_created_total)'
"""
    )
    monkeypatch.setattr(components, "COMPONENTS", components.load(config))
    cluster.bodies["queries"]["sum(vl_streams_created_total)"] = {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "12"]}]},
    }
    assert lines() == [*NINE, "Logs Healthy 2/2 up 12 log streams"]


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        ('name = "X"', "component 1 (X) has no alerts, monitors, queries, signal"),
        (
            'name = "X"\nmonitors = ["argocd/kyverno"]\nalerts = []\nsignal = ""\n'
            "[component.queries]\nq = 'up'",
            "monitors must be a list of Kind/namespace/name, not ['argocd/kyverno']",
        ),
        (
            'name = "X"\nmonitors = ["Probe/a/b"]\nalerts = []\nsignal = "{n}"\n'
            "[component.queries]\nq = 'up'",
            "signal uses ['n'], the queries are ['q']",
        ),
        (
            'name = "X"\nmonitors = ["Probe/a/b"]\nalerts = []\nsignal = "{q"\n'
            "[component.queries]\nq = 'up'",
            "signal is not a format string",
        ),
        (
            'name = "X"\nmonitors = ["Probe/a/b"]\nalerts = []\nsignal = "{q:d}"\n'
            "[component.queries]\nq = 'up'",
            "signal is not a format string for the queries' numbers: Unknown format code 'd'",
        ),
        (
            'name = "X"\nmonitors = ["Probe/a/b"]\nalerts = "A"\nsignal = "{q}"\n'
            "[component.queries]\nq = 'up'",
            "alerts must be a list of alert names",
        ),
        (
            'name = "X"\nmonitors = ["Probe/a/b"]\nalerts = []\nsignal = "{q}"\ndashbord = "d"\n'
            "[component.queries]\nq = 'up'",
            "has unknown keys dashbord",
        ),
        (
            'name = "X"\nmonitors = ["Probe/a/b"]\nalerts = []\nsignal = "{q}"\nempty = 0\n'
            "[component.queries]\nq = 'up'",
            "name, signal, dashboard and empty must be strings",
        ),
        (
            'name = "X"\nmonitors = ["Probe/a/b"]\nalerts = []\nsignal = "{q}"\nqueries = {}',
            "queries must be a table of PromQL strings",
        ),
    ],
)
def test_a_broken_config_stops_the_app_saying_what_is_wrong(
    tmp_path: Path, entry: str, problem: str
) -> None:
    config = tmp_path / "components.toml"
    config.write_text(f"[[component]]\n{entry}\n")
    with pytest.raises(ValueError, match=re.escape(problem)):
        components.load(config)


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("", "no [[component]]"),
        ("[[component]\n", "Expected"),
        ("component = [1]", "(no name) is not a table"),
    ],
)
def test_a_config_without_components_stops_the_app(tmp_path: Path, text: str, problem: str) -> None:
    config = tmp_path / "components.toml"
    config.write_text(text)
    with pytest.raises(ValueError, match=re.escape(problem)):
        components.load(config)
