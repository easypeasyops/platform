"""Restarts, OOM kills and memory headroom, over app/containers.py's queries as Prometheus
answered them on the laptop cluster (tests/fixtures/containers.json, 2026-11-17 21:37 PST:
the node up 8 h, cert-manager's controller restarted 3 times in the hour, no OOM kill). The
other cases are that recording with argocd's answers edited here: a crash loop, an OOM kill
inside and outside the hour, high memory, the node just booted, the queries failing."""

import json
import re
from typing import Any

import pytest

from app import containers, monitors
from tests import test_status
from tests.test_overview import page, tiles
from tests.test_replicas import groups
from tests.test_status import FIXTURES, Cluster, read

cluster = test_status.cluster  # the recording, behind both APIs, for every test here
RECORDED_AT = 1794980276.212  # the recording's evaluation time
REDIS = "argocd-redis-5f476768f6-kmpbq/redis"  # argocd's container closest to its limit
ARGOCD = "argocd Healthy Platform · 6/6 up · pods 5/5 ready · {} · 0 alerts"


@pytest.fixture(autouse=True)
def recording(cluster: Cluster) -> None:
    cluster.bodies["containers"] = json.loads((FIXTURES / "containers.json").read_text())


def answer(cluster: Cluster, key: str, series: list[dict[str, Any]]) -> None:
    """Edit the recording: `key`'s query answers `series` for argocd, instead of its own."""
    result: list[dict[str, Any]] = cluster.bodies["containers"][key]["data"]["result"]
    result[:] = [s for s in result if s["metric"].get("namespace") != "argocd"] + series


def argocd(value: float, **labels: str) -> dict[str, Any]:
    return {"metric": {"namespace": "argocd", **labels}, "value": [RECORDED_AT, str(value)]}


def oom_killed(seconds_ago: int) -> dict[str, Any]:
    return argocd(RECORDED_AT - seconds_ago, pod="argocd-repo-server-0", container="repo-server")


def test_no_restarts_says_so_with_the_memory_closest_to_its_limit() -> None:
    model = read()["containers"]
    assert model["window"] == 3600
    assert model["namespaces"]["argocd"] == {
        "restarts": 0,
        "oom": [],
        "memory": {"used": pytest.approx(0.485, abs=0.001), "container": REDIS},
    }
    assert model["namespaces"]["cert-manager"]["restarts"] == 3
    assert tiles(page())["argocd"][::2] == (
        "ok",
        ARGOCD.format("restarts 0 in 1 h · memory 49% of limit"),
    )
    assert groups(page("/monitors"))["argocd"] == (
        "Platform pods 5/5 ready restarts 0 in 1 h · memory 49% of limit 4 monitors"
    )


def test_a_crash_loop_turns_the_tile_alert(cluster: Cluster) -> None:
    answer(cluster, "restarts", [argocd(6)])
    assert tiles(page())["argocd"][::2] == (
        "warn",
        ARGOCD.format("restarts 6 in 1 h · memory 49% of limit").replace("Healthy", "Alert"),
    )
    text = page("/monitors")
    assert groups(text)["argocd"] == (
        "Platform pods 5/5 ready restarts 6 in 1 h · memory 49% of limit · Alert 4 monitors"
    )
    assert '<span class="pill warn">restarts 6' in text


def test_an_oom_kill_in_the_hour_turns_the_tile_alert(cluster: Cluster) -> None:
    answer(cluster, "restarts", [argocd(1)])
    answer(cluster, "oom", [oom_killed(600)])
    assert read()["containers"]["namespaces"]["argocd"]["oom"] == [
        "argocd-repo-server-0/repo-server"
    ]
    assert tiles(page())["argocd"][::2] == (
        "warn",
        ARGOCD.format("restarts 1 in 1 h · OOM killed · memory 49% of limit").replace(
            "Healthy", "Alert"
        ),
    )


def test_an_oom_kill_over_an_hour_ago_no_longer_counts(cluster: Cluster) -> None:
    answer(cluster, "oom", [oom_killed(3601)])
    assert read()["containers"]["namespaces"]["argocd"]["oom"] == []
    assert tiles(page())["argocd"][::2] == (
        "ok",
        ARGOCD.format("restarts 0 in 1 h · memory 49% of limit"),
    )


def test_high_memory_is_shown_not_alerted_on(cluster: Cluster) -> None:
    answer(cluster, "memory", [argocd(0.95, pod="argocd-redis-0", container="redis")])
    assert tiles(page())["argocd"][::2] == (
        "ok",
        ARGOCD.format("restarts 0 in 1 h · memory 95% of limit (near it)"),
    )


def test_restarts_right_after_the_node_s_boot_are_not_counted(cluster: Cluster) -> None:
    """Waking the laptop after a night: the node booted 5 min ago, and every pod restarted."""
    answer(cluster, "restarts", [argocd(6)])
    cluster.bodies["containers"]["uptime"]["data"]["result"][0]["value"][1] = "300"
    assert tiles(page())["argocd"][::2] == (
        "ok",
        ARGOCD.format("restarts not counted yet: the node has just booted · memory 49% of limit"),
    )
    queries = [r.url.params.get("query", "") for r in cluster.requests]
    assert not any("restarts_total" in q for q in queries)


def test_after_the_grace_restarts_count_from_its_end(cluster: Cluster) -> None:
    """Booted 30 min ago: the last 20 min count, the boot's 10 don't."""
    answer(cluster, "restarts", [argocd(2)])
    cluster.bodies["containers"]["uptime"]["data"]["result"][0]["value"][1] = "1800.4"
    assert tiles(page())["argocd"][0] == "warn"
    assert "restarts 2 in 20 min" in tiles(page())["argocd"][2]
    queries = [r.url.params.get("query", "") for r in cluster.requests]
    assert containers.RESTARTS.format(1200) in queries


def test_no_node_exporter_counts_the_whole_hour(cluster: Cluster) -> None:
    cluster.bodies["containers"]["uptime"]["data"]["result"] = []
    assert read()["containers"]["window"] == 3600


def test_the_queries_never_answering_leave_the_tile_unknown_never_healthy(
    cluster: Cluster,
) -> None:
    cluster.down = {"containers"}
    assert read()["containers"] is None
    text = page()
    assert "Prometheus's query API has not answered yet: the restarts and memory are unknown." in (
        text
    )
    assert tiles(text)["argocd"][::2] == (
        "unknown",
        ARGOCD.format("restarts unknown").replace("Healthy", "Unknown"),
    )
    assert re.search(r'<span class="pill unknown">restarts unknown</span>', page("/monitors"))


def test_the_queries_failing_keep_the_last_answer_and_say_since_when(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitors, "TTL", 0)
    page()
    cluster.down = {"containers"}
    text = page()
    assert "Prometheus's query API has not answered since" in text
    assert "the restarts and memory below are what it said then." in text
    assert "restarts 0 in 1 h · memory 49% of limit" in tiles(text)["argocd"][2]
