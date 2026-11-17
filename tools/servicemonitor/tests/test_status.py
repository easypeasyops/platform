"""Alerts and SLO budgets over responses recorded from the laptop cluster (tests/fixtures/:
`alerts.json` and `slo_budget.json` as Prometheus answered, `more_monitors.json` the
monitors those are about that test_monitors.py's recording leaves out, with their
namespace and targets). Three alerts were firing, none of them a test alert; the other
cases are the recording with one thing changed, here."""

import asyncio
import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import history, monitors, status
from app.main import app

FIXTURES = Path(__file__).parent / "fixtures"
PATHS = {
    "/api/v1/namespaces": "namespaces",
    "/apis/monitoring.coreos.com/v1/servicemonitors": "servicemonitors",
    "/apis/monitoring.coreos.com/v1/podmonitors": "podmonitors",
    "/apis/monitoring.coreos.com/v1/probes": "probes",
    "/api/v1/targets": "targets",
    "/api/v1/alerts": "alerts",
    "/api/v1/query": "slo_budget",
    "/api/v1/query_range": "history",  # one recording per query: tests/test_history.py
}
DEPLOY = Path(__file__).parents[1] / "deploy"
GENERATOR = Path(__file__).parents[3] / "scripts/gen_slo_rules.py"


class Cluster:
    """Answers both APIs from the recordings. A test changes `bodies`, or names a
    recording in `down`."""

    def __init__(self) -> None:
        self.bodies: dict[str, Any] = {
            name: json.loads((FIXTURES / f"{name}.json").read_text()) for name in PATHS.values()
        }
        more = json.loads((FIXTURES / "more_monitors.json").read_text())
        self.bodies["namespaces"]["items"] += more["namespaces"]
        self.bodies["servicemonitors"]["items"] += more["servicemonitors"]
        self.targets += more["targets"]
        self.down: set[str] = set()
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        name = PATHS[request.url.path]
        if name in self.down:
            raise httpx.ConnectError("connection refused")
        if name == "history":
            key = {query: key for key, query in history.QUERIES.items()}[
                request.url.params["query"]
            ]
            return httpx.Response(200, json=self.bodies[name][key])
        return httpx.Response(200, json=self.bodies[name])

    @property
    def targets(self) -> list[dict[str, Any]]:
        targets: list[dict[str, Any]] = self.bodies["targets"]["data"]["activeTargets"]
        return targets

    @targets.setter
    def targets(self, targets: list[dict[str, Any]]) -> None:
        self.bodies["targets"]["data"]["activeTargets"] = targets

    @property
    def alerts(self) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = self.bodies["alerts"]["data"]["alerts"]
        return alerts

    @property
    def budgets(self) -> list[dict[str, Any]]:
        budgets: list[dict[str, Any]] = self.bodies["slo_budget"]["data"]["result"]
        return budgets


@pytest.fixture(autouse=True)
def cluster(monkeypatch: pytest.MonkeyPatch) -> Iterator[Cluster]:
    cluster = Cluster()
    monkeypatch.setattr(monitors, "transport", httpx.MockTransport(cluster))
    for module in (monitors, status):
        monkeypatch.setattr(module, "_cache", None)
        monkeypatch.setattr(module, "_last_good", {})
    monkeypatch.setattr(history, "_cache", None)
    yield cluster


def read() -> dict[str, Any]:
    return asyncio.run(status.read())


def monitor(model: dict[str, Any], namespace: str, name: str) -> dict[str, Any]:
    (found,) = (
        m
        for ns in model["namespaces"]
        if ns["name"] == namespace
        for m in ns["monitors"]
        if m["name"] == name
    )
    return found  # type: ignore[no-any-return]


def matches(model: dict[str, Any], namespace: str, name: str) -> list[tuple[str, str]]:
    return [(a["name"], a["match"]) for a in monitor(model, namespace, name)["alerts"]]


def test_the_summary_line() -> None:
    model = read()
    assert {source: state["stale"] for source, state in model["sources"].items()} == {
        "kubernetes": False,
        "prometheus": False,
        "alerts": False,
        "slo": False,
    }
    assert model["summary"] == {
        "monitors": 9,
        "targets_up": 14,
        "targets_down": 0,
        "alerts_firing": 2,
        "lowest_budget": {
            "namespace": "aipostit-prod",
            "slo": "aipostit-latency",
            "remaining": -15.013213661877312,
        },
    }


def test_a_target_down_is_counted(cluster: Cluster) -> None:
    cluster.targets[0]["health"] = "down"
    summary = read()["summary"]
    assert (summary["targets_up"], summary["targets_down"]) == (13, 1)


def test_an_alert_goes_to_the_monitors_of_its_namespace_and_to_the_one_of_its_job() -> None:
    model = read()
    # Its job is kps-prometheus, in monitoring: that monitor by job, the namespace's others
    # by namespace.
    assert matches(model, "monitoring", "kps-prometheus") == [
        ("PrometheusDuplicateTimestamps", "job")
    ]
    for name in ("kps-alertmanager", "kube-state-metrics"):
        assert matches(model, "monitoring", name) == [
            ("PrometheusDuplicateTimestamps", "namespace")
        ]
    # A failed Job in aipostit-prod, seen by kube-state-metrics: the alert carries
    # kube-state-metrics's job, and is not about kube-state-metrics.
    assert matches(model, "aipostit-prod", "aipostit") == [("KubeJobFailed", "namespace")]
    assert matches(model, "aipostit-staging", "aipostit") == []
    assert matches(model, "argocd", "kyverno") == []
    # Watchdog has no namespace, but always fires: it is left out, not unattached.
    assert model["unattached"] == []
    assert model["summary"]["alerts_firing"] == 2


def test_an_alert_has_what_a_link_to_it_needs() -> None:
    (alert,) = monitor(read(), "aipostit-prod", "aipostit")["alerts"]
    assert alert["labels"]["job_name"] == "aipostit-sync-buffer-29913435"
    del alert["labels"]
    assert alert == {
        "name": "KubeJobFailed",
        "severity": "warning",
        "active_at": "2026-11-16T21:35:28.689173717Z",
        "summary": "Job failed to complete.",
        "runbook_url": "https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubejobfailed",
        "prometheus_path": "/alerts?search=KubeJobFailed",
        "match": "namespace",
    }


def test_the_namespace_is_where_the_monitor_scrapes_not_where_it_lives(cluster: Cluster) -> None:
    cluster.alerts[0]["labels"]["namespace"] = "kyverno"
    model = read()
    assert matches(model, "argocd", "kyverno") == [("KubeJobFailed", "namespace")]
    assert matches(model, "aipostit-prod", "aipostit") == []
    cluster.alerts[0]["labels"]["namespace"] = "argocd"
    monitors._cache = status._cache = None
    model = read()
    assert matches(model, "argocd", "kyverno") == []
    assert matches(model, "argocd", "front-door-grafana-localhost") == [
        ("KubeJobFailed", "namespace")
    ]


def test_a_monitor_without_targets_gets_its_own_namespaces_alerts(cluster: Cluster) -> None:
    cluster.targets = [t for t in cluster.targets if t["labels"]["namespace"] != "aipostit-prod"]
    assert matches(read(), "aipostit-prod", "aipostit") == [("KubeJobFailed", "namespace")]


def test_the_instance_tells_apart_monitors_that_share_a_job(cluster: Cluster) -> None:
    probes = ("front-door-aipostit-localhost", "front-door-grafana-localhost")
    cluster.alerts[0]["labels"] = {
        "alertname": "FrontDoorDown",
        "namespace": "argocd",
        "job": "front-door",
        "instance": "http://grafana.localhost/api/health",
    }
    model = read()
    assert [matches(model, "argocd", probe)[0][1] for probe in probes] == ["namespace", "job"]
    # Without it, the job is both probes'.
    del cluster.alerts[0]["labels"]["instance"]
    monitors._cache = status._cache = None
    model = read()
    assert [matches(model, "argocd", probe)[0][1] for probe in probes] == ["job", "job"]


def test_an_alert_in_a_namespace_nothing_is_scraped_in_is_unattached(cluster: Cluster) -> None:
    cluster.alerts[0]["labels"]["namespace"] = "backups"
    model = read()
    assert [a["name"] for a in model["unattached"]] == ["KubeJobFailed"]
    assert model["summary"]["alerts_firing"] == 2


def test_a_pending_alert_is_not_firing(cluster: Cluster) -> None:
    cluster.alerts[0]["state"] = "pending"
    model = read()
    assert matches(model, "aipostit-prod", "aipostit") == []
    assert model["summary"]["alerts_firing"] == 1


def test_the_budget_left_per_slo_and_the_lowest() -> None:
    slo = monitor(read(), "aipostit-prod", "aipostit")["slo"]
    assert [(b["slo"], b["remaining"]) for b in slo["budgets"]] == [
        ("aipostit-availability", 1.0),
        ("aipostit-latency", -15.013213661877312),
    ]
    assert slo["lowest"] == slo["budgets"][1]
    assert slo["reason"] is None
    # What the SLO dashboard's "Error budget remaining" showed for it: -1501%.
    assert round(slo["lowest"]["remaining"] * 100) == -1501


def test_without_an_slo_the_reason(cluster: Cluster) -> None:
    model = read()
    assert monitor(model, "argocd", "kyverno")["slo"] == {
        "budgets": [],
        "lowest": None,
        "reason": "no SLO",
    }
    # As recorded, prod's rules (`sum by (namespace)`, any namespace) give staging a budget.
    assert monitor(model, "aipostit-staging", "aipostit")["slo"]["lowest"]["remaining"] < 0
    cluster.budgets[:] = [
        b for b in cluster.budgets if b["metric"]["namespace"] != "aipostit-staging"
    ]
    monitors._cache = status._cache = None
    assert monitor(read(), "aipostit-staging", "aipostit")["slo"] == {
        "budgets": [],
        "lowest": None,
        # Prod's rules cover staging too: without a series it has had no requests.
        "reason": "no requests in the SLO's window",
    }


def test_a_window_without_requests_has_no_budget_not_a_full_one(cluster: Cluster) -> None:
    for series in cluster.budgets:
        if series["metric"]["namespace"] == "aipostit-prod":
            series["value"][1] = "NaN"
    model = read()
    slo = monitor(model, "aipostit-prod", "aipostit")["slo"]
    assert [b["remaining"] for b in slo["budgets"]] == [None, None]
    assert slo["lowest"] is None
    assert slo["reason"] is None
    assert model["summary"]["lowest_budget"]["namespace"] == "aipostit-staging"


def test_a_query_that_never_answered_leaves_unknown_not_no_alerts_or_a_full_budget(
    cluster: Cluster,
) -> None:
    cluster.down = {"alerts", "slo_budget"}
    model = read()
    for source in ("alerts", "slo"):
        assert model["sources"][source] == {
            "stale": True,
            "as_of": None,
            "error": "connection refused",
        }
    row = monitor(model, "aipostit-prod", "aipostit")
    assert row["alerts"] is None
    assert row["slo"] is None
    assert model["unattached"] is None
    assert model["summary"]["alerts_firing"] is None
    assert model["summary"]["lowest_budget"] is None
    assert model["summary"]["targets_up"] == 14


def test_a_query_that_starts_failing_keeps_its_last_answer_marked_stale(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitors, "TTL", 0)
    fresh = read()
    cluster.down = {"slo_budget"}
    cluster.alerts.clear()
    stale = read()
    assert stale["sources"]["slo"] == {
        "stale": True,
        "as_of": fresh["sources"]["slo"]["as_of"],
        "error": "connection refused",
    }
    assert stale["sources"]["alerts"]["stale"] is False
    assert stale["summary"]["alerts_firing"] == 0
    assert stale["summary"]["lowest_budget"] == fresh["summary"]["lowest_budget"]


def test_an_error_status_is_a_failed_query(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/query":
            return httpx.Response(503)
        return cluster(request)

    monkeypatch.setattr(monitors, "transport", httpx.MockTransport(unavailable))
    model = read()
    assert "503 Service Unavailable" in model["sources"]["slo"]["error"]
    assert monitor(model, "aipostit-prod", "aipostit")["slo"] is None


def test_without_targets_or_monitors_the_counts_are_unknown(cluster: Cluster) -> None:
    cluster.down = {"targets", "namespaces"}
    model = read()
    assert model["namespaces"] is None
    assert model["unattached"] is None
    assert model["summary"] == {
        "monitors": None,
        "targets_up": None,
        "targets_down": None,
        "alerts_firing": 2,
        "lowest_budget": model["summary"]["lowest_budget"],
    }


def test_the_queries_are_cached(cluster: Cluster) -> None:
    read()
    read()
    paths = [r.url.path for r in cluster.requests]
    assert len(paths) == 7
    (query,) = (r for r in cluster.requests if r.url.path == "/api/v1/query")
    assert query.url.params["query"] == status.BUDGET


def test_the_route_serves_the_model() -> None:
    response = TestClient(app).get("/api/status")
    assert response.status_code == 200
    assert set(response.json()) == {"sources", "summary", "namespaces", "unattached"}
    assert response.json()["summary"]["alerts_firing"] == 2


def test_the_budget_series_is_one_the_platforms_generator_records(tmp_path: Path) -> None:
    """The names come from the generator: this runs the checkout's `scripts/gen_slo_rules.py`
    on the tool's own spec, as CI's tools-deploy job does. A generator that renames the
    series, or stops labelling it by `slo` and `namespace`, fails here in the same PR."""
    out = tmp_path / "slo-rules.values.yaml"
    uv = shutil.which("uv")
    assert uv, "uv runs the generator (it pins its own PyYAML)"
    subprocess.run(  # noqa: S603 (fixed arguments, no shell)
        [
            uv,
            "run",
            "--script",
            str(GENERATOR),
            "--spec",
            str(DEPLOY / "prometheus/slos.yaml"),
            "--values",
            "--out",
            str(out),
        ],
        check=True,
    )
    rules = out.read_text()
    spec = (DEPLOY / "prometheus/slos.yaml").read_text()
    rule = re.compile(r"- record: (\S+)\n\s+expr: (.+?)\n\s+labels:\n\s+slo: (\S+)", re.DOTALL)
    recorded = rule.findall(rules)
    budgets = [(expr, slo) for record, expr, slo in recorded if record == status.BUDGET]
    assert [slo for _, slo in budgets] == re.findall(r"- name: (\S+)", spec)
    for expr, slo in budgets:
        # The budget's `namespace` label comes from the series it is computed from.
        (source,) = re.findall(r"^1 - (\S+)\{slo=\"" + slo + r"\"\} / ", expr)
        (source_expr,) = (e for record, e, s in recorded if record == source and s == slo)
        assert "sum by (namespace)" in source_expr
