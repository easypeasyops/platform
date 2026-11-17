"""The Overview tab, over the same recording as the alerts' and budgets' read model
(tests/test_status.py), changed the same way: one thing per test."""

import html
import re

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app.main import app
from tests import test_status
from tests.test_status import Cluster

cluster = test_status.cluster  # the recording, behind both APIs, for every test here
client = TestClient(app)


def page(url: str = "/") -> str:
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    return html.unescape(response.text)


def text_of(markup: str) -> str:
    """What a link says, as a screen reader gets it: no tags, and nothing aria-hidden."""
    said = re.sub(r'<span aria-hidden="true">.*?</span>', "", markup)
    return " ".join(re.sub(r"<.*?>", " ", said).split())


def tiles(text: str) -> dict[str, tuple[str, str, str]]:
    """Each namespace's tile: its class, where it links to, what it says."""
    found = re.findall(r'<a class="tile (\w+)" href="(.*?)">(.*?)</a>', text, re.S)
    return {text_of(body).split()[0]: (tone, href, text_of(body)) for tone, href, body in found}


def squares(text: str, namespace: str) -> list[tuple[str, str, str, str]]:
    """A namespace's squares: class, link, what it says, and the mark that is drawn."""
    row = text[text.index(f'<div class="ns">\n<span class="mono">{namespace}</span>') :]
    return [
        (tone, href, text_of(body), "".join(re.findall(r'aria-hidden="true">(.)<', body)))
        for tone, href, body in re.findall(
            r'<a class="square (\w+)" href="(.*?)" title=".*?">(.*?)</a>',
            row.split("</div>")[0],
        )
    ]


def test_one_tile_per_namespace_apps_first_marked_by_its_worst_monitor() -> None:
    text = page()
    assert tiles(text) == {
        # An alert that is only in the namespace is counted, but it is the budget, spent,
        # that marks these two.
        "aipostit-prod": (
            "warn",
            "/monitors?q=aipostit-prod",
            "aipostit-prod Alert App · 2/2 up · 1 alert · SLO -1501%",
        ),
        "aipostit-staging": (
            "warn",
            "/monitors?q=aipostit-staging",
            "aipostit-staging Alert App · 1/1 up · 0 alerts · SLO -769%",
        ),
        "argocd": ("ok", "/monitors?q=argocd", "argocd Healthy Platform · 6/6 up · 0 alerts"),
        # PrometheusDuplicateTimestamps, on kps-prometheus's job: on the other monitors'
        # rows too, and one alert.
        "monitoring": (
            "warn",
            "/monitors?q=monitoring",
            "monitoring Alert Platform · 5/5 up · 1 alert",
        ),
    }
    assert list(tiles(text)) == ["aipostit-prod", "aipostit-staging", "argocd", "monitoring"]
    # The same shell as the Monitors tab: its summary line, and no notice.
    assert "<b>14 / 14</b> targets up" in text
    assert '<b class="warn">2</b> alerts firing</span>' in text
    assert "has not answered" not in text
    assert "Showing" not in text  # the Monitors tab's count of filtered rows


def test_the_tab_bar_has_both_tabs_and_marks_the_one_shown() -> None:
    current = '<a href="{}" aria-current="page">{}</a>'
    assert current.format("/", "Overview") in page("/")
    assert '<a href="/monitors">Monitors</a>' in page("/")
    for url in ("/monitors", "/apps/aipostit"):
        assert current.format("/monitors", "Monitors") in page(url)
        assert '<a href="/">Overview</a>' in page(url)


def test_a_tile_s_link_is_the_monitors_tab_filtered_to_its_namespace() -> None:
    text = page(tiles(page())["monitoring"][1])
    assert 'name="q" type="search" value="monitoring"' in text
    assert re.findall(r'<h2 class="group">\s*<span class="mono">(.*?)</span>', text) == [
        "monitoring"
    ]


def test_every_target_is_a_square_that_names_its_monitor_endpoint_and_state() -> None:
    text = page()
    assert squares(text, "aipostit-prod") == [
        (
            "up",
            "/monitors?q=aipostit-prod#ServiceMonitor/aipostit-prod/aipostit",
            f"aipostit · http://10.244.0.{pod}:9100/metrics · up",
            "",
        )
        for pod in (32, 8)
    ]
    assert [len(squares(text, ns)) for ns in tiles(text)] == [2, 1, 6, 5]
    assert text.count('<a class="square ') == 14  # the summary line's 14 targets
    # Hovering shows what the link says.
    assert all(
        title == said
        for title, said in re.findall(r'title="(.*?)"><span class="sr">(.*?)</span>', text)
    )
    # The square's link is its monitor's row, which the Monitors tab has under that id.
    assert '<details id="ServiceMonitor/aipostit-prod/aipostit"' in page(
        "/monitors?q=aipostit-prod"
    )


def test_a_target_down_turns_its_tile_down_and_its_square_down_with_a_cross(
    cluster: Cluster,
) -> None:
    down = next(t for t in cluster.targets if t["labels"]["job"] == "kyverno-svc-metrics")
    down["health"] = "down"
    text = page()
    assert tiles(text)["argocd"] == (
        "down",
        "/monitors?q=argocd",
        "argocd Down Platform · 5/6 up · 0 alerts",
    )
    assert '<span class="pill down">Down</span>' in text
    marks = [
        (tone, said.rsplit(" · ", 1)[1], mark) for tone, _, said, mark in squares(text, "argocd")
    ]
    assert sorted(marks) == [("down", "down", "×")] + [("up", "up", "")] * 5
    (cross,) = [said for tone, _, said, _ in squares(text, "argocd") if tone == "down"]
    assert cross.startswith("kyverno · http://10.244.0.")


def test_down_wins_over_an_alert_and_a_spent_budget(cluster: Cluster) -> None:
    next(t for t in cluster.targets if t["labels"]["job"] == "aipostit")["health"] = "down"
    assert tiles(page())["aipostit-prod"][2] == (
        "aipostit-prod Down App · 1/2 up · 1 alert · SLO -1501%"
    )


def test_a_monitor_with_no_targets_turns_its_tile_down(cluster: Cluster) -> None:
    cluster.targets = [t for t in cluster.targets if t["labels"]["job"] != "cloudnative-pg"]
    text = page()
    assert tiles(text)["argocd"][::2] == (
        "down",
        "argocd Down Platform · 5/5 up · 1 monitor without targets · 0 alerts",
    )
    assert len(squares(text, "argocd")) == 5  # a square is a target: it has none


def test_an_alert_that_is_only_in_the_namespace_leaves_the_tile_healthy(cluster: Cluster) -> None:
    """The Monitors tab's rule: KubeJobFailed is on no job that aipostit's monitor scrapes."""
    cluster.budgets[:] = [b for b in cluster.budgets if b["metric"]["namespace"] != "aipostit-prod"]
    assert tiles(page())["aipostit-prod"][::2] == (
        "ok",
        "aipostit-prod Healthy App · 2/2 up · 1 alert",
    )


def test_a_target_prometheus_has_not_scraped_yet_is_unknown_not_up(cluster: Cluster) -> None:
    next(t for t in cluster.targets if t["labels"]["job"] == "aipostit")["health"] = "unknown"
    text = page()
    tone, _, said, mark = squares(text, "aipostit-prod")[0]
    assert (tone, said.rsplit(" · ", 1)[1], mark) == ("unknown", "unknown", "?")
    assert tiles(text)["aipostit-prod"][0] == "down"  # like its row: a target that isn't up


def test_every_tile_and_square_is_a_link_whose_text_says_its_state(cluster: Cluster) -> None:
    targets = cluster.targets
    next(t for t in targets if t["labels"]["job"] == "kyverno-svc-metrics")["health"] = "down"
    next(t for t in targets if t["labels"]["job"] == "aipostit")["health"] = "unknown"
    text = page()
    live = text[text.index('<div id="live">') :].split("</main>")[0]
    links = re.findall(
        r'<a class="(tile|square) \w+" href="/monitors\?q=.*?">(.*?)</a>', live, re.S
    )
    assert [kind for kind, _ in links].count("tile") == 4
    assert [kind for kind, _ in links].count("square") == 14
    for kind, body in links:
        states = (
            ("Healthy", "Alert", "Down", "Unknown") if kind == "tile" else ("up", "down", "unknown")
        )
        assert any(f" {state}" in text_of(body) for state in states), body
    assert {text_of(body).split()[1] for kind, body in links if kind == "tile"} == {"Down", "Alert"}
    assert sorted({text_of(body).split()[-1] for kind, body in links if kind == "square"}) == [
        "down",
        "unknown",
        "up",
    ]


def test_prometheus_never_answering_shows_nothing_as_healthy(cluster: Cluster) -> None:
    cluster.down = {"targets", "alerts", "slo_budget"}
    text = page()
    assert "Stale: see below" in text
    assert "Prometheus has not answered yet: the targets are unknown." in text
    assert "Prometheus's alerts API has not answered yet: the alerts are unknown." in text
    assert {tile[::2] for tile in tiles(text).values()} >= {
        ("unknown", "argocd Unknown Platform · targets unknown · alerts unknown · SLO unknown")
    }
    assert {tone for tone, _, _ in tiles(text).values()} == {"unknown"}
    assert "Healthy" not in text
    assert '<a class="square' not in text
    row = text[text.index('<div class="ns">\n<span class="mono">argocd</span>') :]
    assert text_of(row.split("</div>")[0]) == "argocd unknown"


def test_alerts_never_answering_leaves_a_tile_with_every_target_up_unknown(
    cluster: Cluster,
) -> None:
    cluster.down = {"alerts"}
    text = page()
    assert tiles(text)["argocd"][::2] == (
        "unknown",
        "argocd Unknown Platform · 6/6 up · alerts unknown",
    )
    assert tiles(text)["aipostit-prod"][2].startswith("aipostit-prod Alert ")  # its budget
    assert len(squares(text, "argocd")) == 6  # the targets are known


def test_kubernetes_never_answering_shows_no_tile(cluster: Cluster) -> None:
    cluster.down = {"namespaces", "servicemonitors", "podmonitors", "probes"}
    text = page()
    assert "The Kubernetes API has not answered yet: the monitors are unknown." in text
    assert tiles(text) == {}
    assert "Every scrape target" not in text


def test_the_overview_is_counted_under_its_own_route() -> None:
    labels = {"method": "GET", "route": "/", "status": "200"}
    before = REGISTRY.get_sample_value("http_requests_total", labels) or 0.0
    page()
    assert REGISTRY.get_sample_value("http_requests_total", labels) == before + 1
    assert REGISTRY.get_sample_value(
        "http_request_duration_seconds_count", {"method": "GET", "route": "/"}
    )
