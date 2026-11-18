"""What each environment runs, over Prometheus's answers recorded from the laptop cluster
on 2026-11-17 (tests/fixtures/environments.json, served by tests/conftest.py's Prometheus):
aipostit and java-hello each on one digest in staging and prod, every Application Synced
and Healthy. Each test changes one thing."""

import asyncio
import html
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import environments, monitors
from app.main import app
from tests import test_status
from tests.conftest import Prometheus

cluster = test_status.cluster  # the monitors' recording behind the page, for the page test

APP, ENV = monitors.APP_LABEL, "easypeasyops.github.io/env"
# The namespaces' labels as `kubectl get ns` showed them when the queries were recorded.
LABELS = {
    "aipostit-prod": {APP: "aipostit", ENV: "prod"},
    "aipostit-staging": {APP: "aipostit", ENV: "staging"},
    "java-hello-prod": {APP: "java-hello", ENV: "prod"},
    "java-hello-staging": {APP: "java-hello", ENV: "staging"},
    "deploybot-ops": {APP: "deploybot", ENV: "ops"},
    "servicemonitor-ops": {APP: "servicemonitor", ENV: "ops"},
    "provenance-canary": {APP: "shortly"},  # no environment: not an app's deploy
    "argocd": {},
}
AIPOSTIT = "ghcr.io/easypeasyops/hotel_social_posts/aipostit@sha256:"
JAVA = "ghcr.io/easypeasyops/java-hello/java-hello@sha256:"


@pytest.fixture(autouse=True)
def labels(monkeypatch: pytest.MonkeyPatch, cluster: test_status.Cluster) -> None:
    monkeypatch.setattr(monitors, "_last_good", {"kubernetes": ("now", {"labels": LABELS})})


def read() -> dict[str, Any]:
    return asyncio.run(environments.read())


def row(view: dict[str, Any], app: str) -> dict[str, Any]:
    (found,) = (r for r in view["rows"] if r["app"] == app)
    return found  # type: ignore[no-any-return]


def summary(found: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    """Each environment: its name, its digests with their pods, its Applications' state."""
    return [
        (
            cell["env"],
            cell["digests"] and [(d["short"], d["pods"]) for d in cell["digests"]],
            cell["applications"]
            and [(a["sync"], a["health"], a["tone"]) for a in cell["applications"]],
        )
        for cell in found["cells"]
    ]


def series(prometheus: Prometheus, name: str, namespace: str) -> dict[str, Any]:
    key = "namespace" if name == "images" else "dest_namespace"
    (found,) = (
        s for s in prometheus.bodies[name]["data"]["result"] if s["metric"][key] == namespace
    )
    return found  # type: ignore[no-any-return]


def test_both_environments_on_one_digest() -> None:
    view = read()
    assert view["errors"] == []
    assert [r["app"] for r in view["rows"]] == [
        "aipostit",
        "deploybot",
        "java-hello",
        "servicemonitor",
    ]
    aipostit = row(view, "aipostit")
    assert summary(aipostit) == [
        ("staging", [("sha256:45edbd50cf68", 1)], [("Synced", "Healthy", "ok")]),
        ("prod", [("sha256:45edbd50cf68", 2)], [("Synced", "Healthy", "ok")]),
    ]
    assert aipostit["prod_vs_staging"] == "same"
    assert not any(cell["rolling"] for cell in aipostit["cells"])
    assert row(view, "java-hello")["prod_vs_staging"] == "same"
    # A tool's one environment: nothing to compare.
    assert summary(row(view, "servicemonitor")) == [
        ("ops", [("sha256:94be63e52478", 1)], [("Synced", "Healthy", "ok")])
    ]
    assert row(view, "servicemonitor")["prod_vs_staging"] is None


def test_prod_behind_staging(env_prometheus: Prometheus) -> None:
    series(env_prometheus, "images", "java-hello-staging")["metric"]["image_id"] = JAVA + "ab" * 32
    found = row(read(), "java-hello")
    assert found["prod_vs_staging"] == "differs"
    assert summary(found)[0][1] == [("sha256:abababababab", 1)]


def test_two_digests_in_one_environment(env_prometheus: Prometheus) -> None:
    """A promote rolling out in prod: the old pod and the new one, at once."""
    new = {
        "metric": {"namespace": "aipostit-prod", "image_id": AIPOSTIT + "cd" * 32},
        "value": [0, "1"],
    }
    env_prometheus.bodies["images"]["data"]["result"].append(new)
    found = row(read(), "aipostit")
    prod = found["cells"][1]
    assert prod["rolling"]
    assert [(d["short"], d["pods"]) for d in prod["digests"]] == [
        ("sha256:45edbd50cf68", 2),
        ("sha256:cdcdcdcdcdcd", 1),
    ]
    assert found["prod_vs_staging"] == "differs"  # not on staging's digests alone


def test_an_out_of_sync_application(env_prometheus: Prometheus) -> None:
    series(env_prometheus, "applications", "aipostit-prod")["metric"]["sync_status"] = "OutOfSync"
    found = row(read(), "aipostit")
    assert summary(found)[1][2] == [("OutOfSync", "Healthy", "warn")]
    assert found["prod_vs_staging"] == "same"  # what runs is still the same digest


def test_a_degraded_application_is_down(env_prometheus: Prometheus) -> None:
    series(env_prometheus, "applications", "java-hello-prod")["metric"]["health_status"] = (
        "Degraded"
    )
    assert summary(row(read(), "java-hello"))[1][2] == [("Synced", "Degraded", "down")]


def test_the_images_query_failing_never_reads_as_matching(env_prometheus: Prometheus) -> None:
    env_prometheus.down.add("images")
    view = read()
    assert view["errors"] == ["images: connection refused"]
    found = row(view, "aipostit")
    assert [cell["digests"] for cell in found["cells"]] == [None, None]
    assert found["prod_vs_staging"] == "unknown"
    # The Applications' state is still known.
    assert summary(found)[1][2] == [("Synced", "Healthy", "ok")]
    env_prometheus.down.clear()
    assert read()["errors"] == []  # a failure isn't cached


def test_the_applications_query_failing(env_prometheus: Prometheus) -> None:
    env_prometheus.down.add("applications")
    view = read()
    assert view["errors"] == ["Applications: connection refused"]
    found = row(view, "aipostit")
    assert [cell["applications"] for cell in found["cells"]] == [None, None]
    assert found["prod_vs_staging"] == "same"


def test_no_pod_runs_is_not_a_match(env_prometheus: Prometheus) -> None:
    result = env_prometheus.bodies["images"]["data"]["result"]
    result.remove(series(env_prometheus, "images", "aipostit-staging"))
    found = row(read(), "aipostit")
    assert found["cells"][0]["digests"] == []
    assert found["prod_vs_staging"] == "unknown"


def test_apps_unknown_without_the_kubernetes_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monitors, "_last_good", {})
    assert read()["rows"] is None


def test_the_overview_shows_each_environment() -> None:
    """The page over the monitors' recording, whose namespaces are aipostit's two."""
    response = TestClient(app).get("/")
    assert response.status_code == 200
    text = html.unescape(response.text)
    (panel,) = re.findall(r'<div class="envrow" data-app="aipostit">(.*?)\n</div>', text, re.S)
    said = " ".join(re.sub(r"<.*?>", " ", panel).split())
    assert said == (
        "aipostit staging aipostit-staging sha256:45edbd50cf68 · 1 pod Synced · Healthy"
        " prod aipostit-prod sha256:45edbd50cf68 · 2 pods Synced · Healthy"
        " prod on staging's digest"
    )
    page = TestClient(app).get("/").text
    assert 'title="' + AIPOSTIT in page  # the full digest, on hover
