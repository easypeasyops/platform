"""The alerts and SLO budgets on the Monitors tab, over the same recording as their read
model's tests (tests/test_status.py), changed the same way: one thing per test."""

import html
import re
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app import monitors, status
from app.main import app
from tests import test_status
from tests.test_status import Cluster

cluster = test_status.cluster  # the recording, behind both APIs, for every test here
client = TestClient(app)


def page(url: str = "/monitors") -> str:
    response = client.get(url)
    assert response.status_code == 200
    return html.unescape(response.text)


def rows(text: str) -> list[str]:
    return re.findall(r'<details id="(.*?)"', text)


def row(text: str, monitor: str) -> str:
    """A monitor's row, without the targets it expands to."""
    return text[text.index(f'<details id="{monitor}"') :].split("</summary>")[0]


def alerts(text: str, monitor: str) -> list[tuple[str, str, str]]:
    return re.findall(
        r'<a class="(alert(?: dim)?)" href="(.*?)" title=".*?">(.*?)</a>', row(text, monitor)
    )


def test_the_summary_line_has_the_alerts_firing_and_the_lowest_budget() -> None:
    text = page()
    assert "<b>9</b> monitors" in text
    assert "<b>14 / 14</b> targets up" in text
    # Every firing alert is counted but Watchdog, which always fires: none is on no monitor.
    assert '<b class="warn">2</b> alerts firing</span>' in text
    assert "Watchdog" not in text
    assert (
        '<b class="down">-1501%</b> lowest error budget (aipostit-prod · aipostit-latency)' in text
    )
    assert "has not answered" not in text


def test_an_alert_on_the_monitors_job_links_to_prometheus_and_one_in_its_namespace_is_dimmed() -> (
    None
):
    text = page()
    link = "http://prometheus.localhost:8090/alerts?search=PrometheusDuplicateTimestamps"
    assert alerts(text, "ServiceMonitor/monitoring/kps-prometheus") == [
        ("alert", link, "PrometheusDuplicateTimestamps")
    ]
    assert alerts(text, "ServiceMonitor/monitoring/kps-alertmanager") == [
        ("alert dim", link, "PrometheusDuplicateTimestamps")
    ]
    assert alerts(text, "ServiceMonitor/aipostit-prod/aipostit") == [
        (
            "alert dim",
            "http://prometheus.localhost:8090/alerts?search=KubeJobFailed",
            "KubeJobFailed",
        )
    ]
    assert 'title="In a namespace this monitor scrapes: Job failed to complete."' in text
    kyverno = row(text, "ServiceMonitor/argocd/kyverno")
    assert alerts(text, "ServiceMonitor/argocd/kyverno") == []
    assert '<span class="note">—</span>' in kyverno
    assert "firing PrometheusDuplicateTimestamps" in row(
        text, "ServiceMonitor/monitoring/kps-prometheus"
    )  # what a screen reader hears of the row


def test_a_budget_below_zero_shows_its_number_and_an_empty_bar() -> None:
    aipostit = row(page(), "ServiceMonitor/aipostit-prod/aipostit")
    assert '<span class="slo spent">' in aipostit
    assert "<b>-1501% left</b><span>aipostit-latency</span>" in aipostit
    assert '<span style="width: 0%"></span>' in aipostit
    assert "error budget -1501% left" in aipostit


@pytest.mark.parametrize(
    ("remaining", "left", "width", "spent"),
    [("0.8249", "82%", "82%", False), ("0", "0%", "0%", True), ("1.2", "120%", "100%", False)],
)
def test_the_bar_is_the_budget_left_and_never_overflows(
    cluster: Cluster, remaining: str, left: str, width: str, spent: bool
) -> None:
    for series in cluster.budgets:
        if series["metric"]["namespace"] == "aipostit-prod":
            series["value"][1] = remaining
    aipostit = row(page(), "ServiceMonitor/aipostit-prod/aipostit")
    assert f"<b>{left} left</b>" in aipostit
    assert f'<span style="width: {width}"></span>' in aipostit
    assert ('<span class="slo spent">' in aipostit) is spent


def test_without_a_budget_the_cell_says_why(cluster: Cluster) -> None:
    for series in cluster.budgets:
        if series["metric"]["namespace"] == "aipostit-prod":
            series["value"][1] = "NaN"
    cluster.budgets[:] = [
        b for b in cluster.budgets if b["metric"]["namespace"] != "aipostit-staging"
    ]
    text = page()
    assert '<span class="note">no SLO</span>' in row(text, "ServiceMonitor/argocd/kyverno")
    assert '<span class="note">no requests in the SLO\'s window</span>' in row(
        text, "ServiceMonitor/aipostit-staging/aipostit"
    )
    assert '<span class="note">no requests in the SLO\'s window</span>' in row(
        text, "ServiceMonitor/aipostit-prod/aipostit"
    )
    assert "<b>—</b> no SLO has an error budget" in text


def test_a_job_alert_or_a_spent_budget_is_a_problem_a_namespace_alert_is_not(
    cluster: Cluster,
) -> None:
    text = page("/monitors?show=problems")
    assert rows(text) == [
        "ServiceMonitor/aipostit-prod/aipostit",  # its budget
        "ServiceMonitor/aipostit-staging/aipostit",
        "ServiceMonitor/monitoring/kps-prometheus",  # its alert
    ]
    # Its targets are up: the dot says there is something else.
    assert '<span class="dot warn"' in row(text, "ServiceMonitor/monitoring/kps-prometheus")
    assert '<span class="pill ok">2/2 up</span>' in row(
        text, "ServiceMonitor/monitoring/kps-prometheus"
    )
    # Problems first, in the whole table too.
    monitoring = [r for r in rows(page()) if "/monitoring/" in r]
    assert monitoring[0] == "ServiceMonitor/monitoring/kps-prometheus"
    # With budget left, aipostit-prod has only the failed Job's alert, by namespace.
    for series in cluster.budgets:
        series["value"][1] = "0.5"
    monitors._cache = status._cache = None
    text = page("/monitors?show=problems")
    assert rows(text) == ["ServiceMonitor/monitoring/kps-prometheus"]
    assert '<span class="dot ok"' in row(page(), "ServiceMonitor/aipostit-prod/aipostit")


def test_the_alerts_link_asks_for_where_the_monitor_scrapes_not_where_it_lives(
    cluster: Cluster,
) -> None:
    def link(text: str, monitor: str) -> str:
        (href,) = re.findall(r'href="([^"]*)">Alerts</a>', row(text, monitor))
        return unquote(href)

    text = page()
    query = "http://prometheus.localhost:8090/query?g0.tab=table&g0.expr="
    assert link(text, "ServiceMonitor/argocd/kyverno") == query + 'ALERTS{namespace=~"kyverno"}'
    assert link(text, "PodMonitor/argocd/cloudnative-pg").endswith('{namespace=~"cnpg-system"}')
    next(t for t in cluster.targets if t["labels"]["namespace"] == "kyverno")["labels"][
        "namespace"
    ] = "kyverno-reports"
    monitors._cache = status._cache = None
    assert link(page(), "ServiceMonitor/argocd/kyverno").endswith(
        '{namespace=~"kyverno|kyverno-reports"}'
    )


def test_queries_that_never_answered_show_unknown_not_no_alerts_or_a_full_budget(
    cluster: Cluster,
) -> None:
    cluster.down = {"alerts", "slo_budget"}
    text = page()
    assert "Stale: see below" in text
    assert "Prometheus's alerts API has not answered yet: the alerts are unknown." in text
    assert "Prometheus's query API has not answered yet: the error budgets are unknown." in text
    assert "<b>?</b> alerts unknown" in text
    assert "<b>?</b> error budgets unknown" in text
    assert "alerts firing" not in text
    aipostit = row(text, "ServiceMonitor/aipostit-prod/aipostit")
    assert aipostit.count('<span class="note">unknown</span>') == 2
    assert "<b>14 / 14</b> targets up" in text  # what is known stays
    assert rows(page("/monitors?show=problems")) == []


def test_a_query_that_went_down_keeps_its_last_answer_and_says_since_when(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitors, "TTL", 0)
    (as_of,) = set(re.findall(r'Live · updated <time datetime="(.*?)">', page()))
    cluster.down = {"alerts"}
    text = page()
    assert f'Prometheus\'s alerts API has not answered since <time datetime="{as_of}">' in text
    assert "the alerts below are what it said then." in text
    assert '<b class="warn">2</b> alerts firing' in text
    assert "query API" not in text
