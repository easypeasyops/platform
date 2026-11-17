"""The Monitors tab, over the same recording as the read model's tests
(tests/test_monitors.py), changed the same way: one thing per test. No alert fires here
and no SLO has a budget: tests/test_pages_status.py has those."""

import asyncio
import copy
import html
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app import monitors, pages, status
from app.main import app
from tests import test_monitors
from tests.test_monitors import Cluster

cluster = test_monitors.cluster  # the recording, behind both APIs, for every test here
client = TestClient(app)
NOW = datetime(2026, 11, 16, 22, 4, 40, tzinfo=UTC)  # seconds after the recording
QUIET: dict[str, Any] = {"/api/v1/alerts": {"alerts": []}, "/api/v1/query": {"result": []}}


@pytest.fixture(autouse=True)
def quiet(cluster: Cluster, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prometheus, while it answers, has no alert and no budget series."""

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path not in QUIET:
            return cluster(request)
        if "prometheus" in cluster.down:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"data": QUIET[request.url.path]})

    monkeypatch.setattr(monitors, "transport", httpx.MockTransport(answer))
    monkeypatch.setattr(status, "_cache", None)
    monkeypatch.setattr(status, "_last_good", {})


def page(url: str) -> str:
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    return html.unescape(response.text)


def groups(text: str) -> list[str]:
    return re.findall(r'<h2 class="group">\s*<span class="mono">(.*?)</span>', text)


def rows(text: str) -> list[str]:
    return re.findall(r'<details id="(.*?)"', text)


def row(namespace: str, name: str) -> dict[str, Any]:
    model = test_monitors.read()
    view = pages.view(model, asyncio.run(status.read()), "/monitors", "", "all", None, NOW)
    (found,) = (
        m
        for g in view["groups"]
        if g["name"] == namespace
        for m in g["monitors"]
        if m["name"] == name
    )
    return found  # type: ignore[no-any-return]


def add_aipostit_staging(cluster: Cluster) -> None:
    for body in ("namespaces", "servicemonitors"):
        items = cluster.bodies[body]["items"]
        staging = copy.deepcopy(items[0])
        text = staging["metadata"]
        text["name"] = text["name"].replace("-prod", "-staging")
        if "namespace" in text:
            text["namespace"] = "aipostit-staging"
        items.append(staging)


def test_one_table_grouped_by_namespace_apps_first() -> None:
    text = page("/monitors")
    assert groups(text) == ["aipostit-prod", "argocd", "monitoring"]
    assert re.findall(r'<span class="tag (\w+)">(\w+)</span>', text) == [
        ("app", "App"),
        ("platform", "Platform"),
        ("platform", "Platform"),
    ]
    assert "<b>6</b> monitors (3 ServiceMonitors · 1 PodMonitors · 2 Probes)" in text
    assert "<b>10 / 10</b> targets up" in text
    assert "<b>0</b> down: every target answers" in text
    assert "Showing all 6 monitors" in text
    assert "has not answered" not in text
    assert 'href="http://prometheus.localhost:8090/targets"' in text


def test_a_target_down_puts_its_row_first_with_prometheus_s_error_in_it(cluster: Cluster) -> None:
    down = next(t for t in cluster.targets if t["labels"]["job"] == "kyverno-svc-metrics")
    down["health"] = "down"
    down["lastError"] = 'Get "http://10.244.0.13:8000/metrics": connection refused <eof>'
    raw = client.get("/monitors").text
    text = html.unescape(raw)
    assert rows(text)[1:3] == ["ServiceMonitor/argocd/kyverno", "PodMonitor/argocd/cloudnative-pg"]
    expanded = text[text.index('<details id="ServiceMonitor/argocd/kyverno"') :].split(
        "</details>"
    )[0]
    assert 'class="down"' in expanded
    assert '<span class="pill down">2/3 up</span>' in expanded
    assert (
        '<span class="mono error">Get "http://10.244.0.13:8000/metrics": connection refused <eof>'
        in expanded
    )
    assert "<eof>" not in raw  # Prometheus's text is escaped
    assert '<b class="down">1</b> down: argocd/kyverno' in text
    assert rows(page("/monitors?show=problems")) == ["ServiceMonitor/argocd/kyverno"]


def test_apps_aipostit_shows_only_that_app_s_prod_and_staging(cluster: Cluster) -> None:
    add_aipostit_staging(cluster)
    text = page("/apps/aipostit")
    assert groups(text) == ["aipostit-prod", "aipostit-staging"]
    assert rows(text) == [
        "ServiceMonitor/aipostit-prod/aipostit",
        "ServiceMonitor/aipostit-staging/aipostit",
    ]
    assert "Showing 2 of 7 monitors" in text
    assert 'href="/apps/aipostit?show=problems"' in text  # the buttons stay on the app
    # A name that merely contains the app's isn't the app.
    assert "No monitor matches." in page("/apps/aipost")


def test_the_filter_and_the_buttons_are_in_the_url() -> None:
    text = page("/monitors?q=Kyverno")
    assert rows(text) == ["ServiceMonitor/argocd/kyverno"]
    assert 'name="q" type="search" value="kyverno"' in text
    assert 'href="/monitors?q=kyverno&show=problems"' in text
    assert '<a href="/monitors?q=kyverno" aria-current="true">All</a>' in text
    assert groups(page("/monitors?q=aipostit")) == ["aipostit-prod", "argocd"]  # app, then a probe
    assert groups(page("/monitors?show=apps")) == ["aipostit-prod"]
    text = page("/monitors?show=platform")
    assert groups(text) == ["argocd", "monitoring"]
    assert '<input type="hidden" name="show" value="platform">' in text
    assert '<a href="/monitors?show=platform" aria-current="true">Platform</a>' in text
    assert "No monitor matches." in page("/monitors?show=problems")
    assert groups(page("/monitors?show=nonsense")) == ["aipostit-prod", "argocd", "monitoring"]


def test_a_row_expands_to_its_targets_selector_interval_and_job() -> None:
    aipostit = row("aipostit-prod", "aipostit")
    assert (aipostit["status"], aipostit["tone"], aipostit["last"]) == ("2/2 up", "ok", "7 s ago")
    assert aipostit["targets"][0] == {
        "endpoint": "http://10.244.0.32:9100/metrics",
        "health": "up",
        "last": "10 s ago",
        "duration": "7 ms",
        "error": "",
    }
    assert (
        aipostit["selector"]
        == "app.kubernetes.io/instance=aipostit, app.kubernetes.io/name=aipostit"
    )
    assert (aipostit["interval"], aipostit["job"]) == ("15s", "aipostit")
    kyverno = row("argocd", "kyverno")
    assert kyverno["selector"].endswith("app.kubernetes.io/part-of=kyverno in kyverno")
    assert kyverno["job"] == "kyverno-reports-controller-metrics, kyverno-svc-metrics"
    assert row("monitoring", "kps-alertmanager")["interval"] == "Prometheus's default"
    probe = row("argocd", "front-door-grafana-localhost")
    assert (probe["selector"], probe["interval"], probe["job"]) == ("—", "60s", "front-door")
    text = page("/monitors")
    assert 'Selector <span class="mono">app.kubernetes.io/instance=aipostit,' in text
    assert "<summary" in text  # a native <details>: Enter opens it


def test_other_selectors(cluster: Cluster) -> None:
    spec = cluster.bodies["servicemonitors"]["items"][1]["spec"]
    spec["selector"] = {
        "matchExpressions": [{"key": "tier", "operator": "In", "values": ["a", "b"]}]
    }
    spec["namespaceSelector"] = {"any": True}
    cluster.bodies["podmonitors"]["items"][0]["spec"]["selector"] = {}
    assert row("argocd", "kyverno")["selector"] == "tier In a,b in every namespace"
    assert row("argocd", "kyverno")["links"]["Logs"] is None
    assert row("argocd", "cloudnative-pg")["selector"] == "everything in cnpg-system"


def test_the_links(cluster: Cluster) -> None:
    del cluster.bodies["podmonitors"]["items"][0]["metadata"]["annotations"]
    links = row("aipostit-prod", "aipostit")["links"]
    assert links["Dashboard"] == "http://grafana.localhost:8090/dashboards?query=aipostit"
    assert unquote(links["Alerts"]) == (
        'http://prometheus.localhost:8090/query?g0.tab=table&g0.expr=ALERTS{namespace=~"aipostit-prod"}'
    )
    assert links["Argo CD"] == "http://argocd.localhost:8090/applications/argocd/aipostit-prod"
    assert links["Logs"].startswith("http://grafana.localhost:8090/explore?schemaVersion=1&")
    assert '"expr":"{kubernetes.pod_namespace=~\\"aipostit-prod\\"}"' in unquote(links["Logs"])
    # A platform monitor: the Application that deployed it, and the logs of where it selects.
    kyverno = row("argocd", "kyverno")["links"]
    assert kyverno["Dashboard"].endswith("/dashboards?query=kyverno")
    assert kyverno["Argo CD"].endswith("/applications/argocd/platform")
    assert 'pod_namespace=~\\"kyverno\\"' in unquote(kyverno["Logs"])
    assert row("monitoring", "kps-alertmanager")["links"]["Argo CD"].endswith("/argocd/monitoring")
    # What can't be worked out is left out: a Probe has no pods, and nothing says which
    # Application a monitor without Argo CD's annotation belongs to.
    assert row("argocd", "front-door-grafana-localhost")["links"]["Logs"] is None
    assert row("argocd", "cloudnative-pg")["links"]["Argo CD"] is None
    text = page("/monitors")
    cnpg = text[text.index('<details id="PodMonitor/argocd/cloudnative-pg"') :].split("</summary>")[
        0
    ]
    assert re.findall(r">(\w[\w ]*)</a>", cnpg) == ["Dashboard", "Logs", "Alerts"]


def test_a_monitor_with_no_targets_is_a_problem(cluster: Cluster) -> None:
    cluster.targets[:] = [t for t in cluster.targets if t["labels"]["job"] != "cloudnative-pg"]
    text = page("/monitors?show=problems")
    assert rows(text) == ["PodMonitor/argocd/cloudnative-pg"]
    assert '<span class="pill warn">no targets</span>' in text
    assert "Prometheus scrapes nothing for this monitor" in text
    assert "<b>0</b> down: every target answers" in text  # none is down: the row says the rest


def test_prometheus_never_answering_shows_no_monitor_as_healthy(cluster: Cluster) -> None:
    cluster.down = {"prometheus"}
    text = page("/monitors")
    assert "Stale: see below" in text
    assert "Prometheus has not answered yet: the targets are unknown." in text
    assert "connection refused" in text
    assert "targets up" not in text
    assert text.count('<span class="pill unknown">unknown</span>') == 6
    assert len(rows(page("/monitors?show=problems"))) == 6


def test_kubernetes_never_answering_shows_no_table(cluster: Cluster) -> None:
    cluster.down = {"kubernetes"}
    text = page("/monitors")
    assert "The Kubernetes API has not answered yet: the monitors are unknown." in text
    assert "<details" not in text
    assert "No monitor matches." not in text
    assert "Showing" not in text


def test_a_source_that_went_down_says_which_and_since_when(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitors, "TTL", 0)
    fresh = page("/monitors")
    (as_of,) = set(re.findall(r'Live · updated <time datetime="(.*?)">', fresh))
    cluster.down = {"prometheus"}
    text = page("/monitors")
    assert f'Prometheus has not answered since <time datetime="{as_of}">' in text
    assert "the targets below are what it said then." in text
    assert "Kubernetes" not in text
    assert "<b>10 / 10</b> targets up" in text


@pytest.mark.parametrize(
    ("then", "ago"),
    [
        ("2026-11-16T22:04:39.5Z", "0 s ago"),
        ("2026-11-16T22:02:41Z", "119 s ago"),
        ("2026-11-16T21:04:40Z", "60 min ago"),
        ("2026-11-16T19:04:40Z", "3 h ago"),
        ("0001-01-01T00:00:00Z", "never"),  # a target Prometheus hasn't scraped yet
    ],
)
def test_ages(then: str, ago: str) -> None:
    assert pages._ago(then, NOW) == ago


def test_an_app_s_page_is_counted_under_the_route_not_the_app() -> None:
    labels = {"method": "GET", "route": "/apps/{app}", "status": "200"}
    before = REGISTRY.get_sample_value("http_requests_total", labels) or 0.0
    page("/apps/aipostit")
    page("/apps/deploybot")
    assert REGISTRY.get_sample_value("http_requests_total", labels) == before + 2
