"""The read model over API responses recorded from the laptop cluster (tests/fixtures/:
the Kubernetes lists trimmed to a few monitors and their name, namespace and spec, the
targets to those monitors' scrape pools). The cluster had every target up and every
monitor scraping, so the other cases are the recording with one thing changed, here."""

import asyncio
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import certifi
import httpx
import pytest
from fastapi.testclient import TestClient

from app import monitors
from app.main import app

FIXTURES = Path(__file__).parent / "fixtures"
PATHS = {
    "/api/v1/namespaces": "namespaces",
    "/apis/monitoring.coreos.com/v1/servicemonitors": "servicemonitors",
    "/apis/monitoring.coreos.com/v1/podmonitors": "podmonitors",
    "/apis/monitoring.coreos.com/v1/probes": "probes",
    "/api/v1/targets": "targets",
}


class Cluster:
    """Answers both APIs from the recording. A test changes `bodies`, or names a source
    in `down`."""

    def __init__(self) -> None:
        self.bodies: dict[str, Any] = {
            name: json.loads((FIXTURES / f"{name}.json").read_text()) for name in PATHS.values()
        }
        self.down: set[str] = set()
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        name = PATHS[request.url.path]
        if ("prometheus" if name == "targets" else "kubernetes") in self.down:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json=self.bodies[name])

    @property
    def targets(self) -> list[dict[str, Any]]:
        targets: list[dict[str, Any]] = self.bodies["targets"]["data"]["activeTargets"]
        return targets


@pytest.fixture(autouse=True)
def cluster(monkeypatch: pytest.MonkeyPatch) -> Iterator[Cluster]:
    cluster = Cluster()
    monkeypatch.setattr(monitors, "transport", httpx.MockTransport(cluster))
    monkeypatch.setattr(monitors, "_cache", None)
    monkeypatch.setattr(monitors, "_last_good", {})
    yield cluster


def read() -> dict[str, Any]:
    return asyncio.run(monitors.read())


def monitor(model: dict[str, Any], namespace: str, name: str) -> dict[str, Any]:
    (found,) = (
        m
        for ns in model["namespaces"]
        if ns["name"] == namespace
        for m in ns["monitors"]
        if m["name"] == name
    )
    return found  # type: ignore[no-any-return]


def test_every_monitor_with_its_targets_up() -> None:
    model = read()
    assert model["sources"]["kubernetes"]["stale"] is False
    assert model["sources"]["prometheus"]["stale"] is False
    assert [(ns["name"], ns["owner"]) for ns in model["namespaces"]] == [
        ("aipostit-prod", "app"),
        ("argocd", "platform"),
        ("monitoring", "platform"),
    ]
    assert [(m["kind"], m["name"]) for m in model["namespaces"][1]["monitors"]] == [
        ("PodMonitor", "cloudnative-pg"),
        ("Probe", "front-door-aipostit-localhost"),
        ("Probe", "front-door-grafana-localhost"),
        ("ServiceMonitor", "kyverno"),
    ]
    aipostit = monitor(model, "aipostit-prod", "aipostit")
    assert len(aipostit["targets"]) == 2
    assert aipostit["targets"][0] == {
        "scrape_pool": "serviceMonitor/aipostit-prod/aipostit/0",
        "endpoint": "http://10.244.0.32:9100/metrics",
        "health": "up",
        "last_scrape": "2026-11-16T22:04:29.08962205Z",
        "duration_seconds": 0.007227333,
        "last_error": "",
    }
    assert model["unclaimed"] == []
    every = [t for ns in model["namespaces"] for m in ns["monitors"] for t in m["targets"]]
    assert len(every) == 10
    assert {t["health"] for t in every} == {"up"}


def test_the_join_is_on_the_scrape_pool_not_the_job_and_namespace_labels(
    cluster: Cluster,
) -> None:
    model = read()
    # A monitor in argocd whose targets are in the kyverno namespace, under two jobs.
    labels = {
        (t["labels"]["namespace"], t["labels"]["job"])
        for t in cluster.targets
        if t["scrapePool"] == "serviceMonitor/argocd/kyverno/0"
    }
    assert labels == {
        ("kyverno", "kyverno-svc-metrics"),
        ("kyverno", "kyverno-reports-controller-metrics"),
    }
    assert len(monitor(model, "argocd", "kyverno")["targets"]) == 3
    # Two probes with the same job and namespace labels: one target each.
    for probe in ("front-door-aipostit-localhost", "front-door-grafana-localhost"):
        assert len(monitor(model, "argocd", probe)["targets"]) == 1
    # One monitor with two endpoints is two scrape pools.
    pools = [t["scrape_pool"] for t in monitor(model, "monitoring", "kps-alertmanager")["targets"]]
    assert pools == [
        "serviceMonitor/monitoring/kps-alertmanager/0",
        "serviceMonitor/monitoring/kps-alertmanager/1",
    ]


def test_a_target_down(cluster: Cluster) -> None:
    down = next(t for t in cluster.targets if t["labels"]["job"] == "aipostit")
    down["health"] = "down"
    down["lastError"] = 'Get "http://10.244.0.32:9100/metrics": connection refused'
    targets = monitor(read(), "aipostit-prod", "aipostit")["targets"]
    assert [t["health"] for t in targets] == ["down", "up"]
    assert targets[0]["last_error"].endswith("connection refused")


def test_a_monitor_with_no_targets(cluster: Cluster) -> None:
    cluster.targets[:] = [t for t in cluster.targets if t["labels"]["job"] != "cloudnative-pg"]
    model = read()
    assert monitor(model, "argocd", "cloudnative-pg")["targets"] == []
    assert model["unclaimed"] == []


def test_a_target_no_monitor_claims(cluster: Cluster) -> None:
    items = cluster.bodies["servicemonitors"]["items"]
    items[:] = [item for item in items if item["metadata"]["name"] != "kyverno"]
    model = read()
    assert [t["scrape_pool"] for t in model["unclaimed"]] == ["serviceMonitor/argocd/kyverno/0"] * 3
    assert "kyverno" not in [m["name"] for m in model["namespaces"][1]["monitors"]]


def test_prometheus_down_from_the_start_leaves_targets_unknown_not_empty(
    cluster: Cluster,
) -> None:
    cluster.down = {"prometheus"}
    model = read()
    assert model["sources"]["prometheus"] == {
        "stale": True,
        "as_of": None,
        "error": "connection refused",
    }
    assert model["sources"]["kubernetes"]["stale"] is False
    assert monitor(model, "aipostit-prod", "aipostit")["targets"] is None
    assert model["unclaimed"] is None


def test_kubernetes_down_from_the_start_leaves_the_namespaces_unknown(cluster: Cluster) -> None:
    cluster.down = {"kubernetes"}
    model = read()
    assert model["sources"]["kubernetes"]["stale"] is True
    assert model["sources"]["prometheus"]["stale"] is False
    assert model["namespaces"] is None
    assert model["unclaimed"] is None


def test_a_source_that_goes_down_keeps_its_last_answer_marked_stale(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitors, "TTL", 0)
    fresh = read()
    cluster.down = {"kubernetes", "prometheus"}
    stale = read()
    for source in ("kubernetes", "prometheus"):
        assert stale["sources"][source] == {
            "stale": True,
            "as_of": fresh["sources"][source]["as_of"],
            "error": "connection refused",
        }
    assert stale["namespaces"] == fresh["namespaces"]


def test_an_error_status_is_a_source_down(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbidden = httpx.MockTransport(lambda request: httpx.Response(403))
    monkeypatch.setattr(monitors, "transport", forbidden)
    model = read()
    assert "403 Forbidden" in model["sources"]["kubernetes"]["error"]
    assert model["namespaces"] is None


def test_the_answer_is_cached(cluster: Cluster) -> None:
    assert read() is read()
    assert len(cluster.requests) == 5


def test_in_a_pod_it_sends_the_service_account_token(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "token").write_text("the-token\n")
    shutil.copy(certifi.where(), tmp_path / "ca.crt")
    monkeypatch.setattr(monitors, "SERVICE_ACCOUNT", tmp_path)
    read()
    sent = {r.url.path: r.headers.get("authorization") for r in cluster.requests}
    assert sent.pop("/api/v1/targets") is None  # Prometheus never sees the token
    assert set(sent.values()) == {"Bearer the-token"}


def test_the_route_serves_the_model() -> None:
    response = TestClient(app).get("/api/monitors")
    assert response.status_code == 200
    assert set(response.json()) == {"sources", "namespaces", "unclaimed"}
    assert len(response.json()["namespaces"]) == 3
