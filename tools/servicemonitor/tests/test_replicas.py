"""Pods ready against desired, over app/status.py's REPLICAS queries as Prometheus answered
them on the laptop cluster (tests/fixtures/replicas.json, 2026-11-16 23:34 PST): every
Deployment and StatefulSet at full strength, so `short` and `none` answered no series.
The other cases are that recording with one namespace's numbers edited here: one short past
the grace period (`short` answers it), a rollout's brief dip inside it (fewer ready, `short`
still empty), none ready (`none` answers it)."""

import re
from typing import Any

import pytest

from app import monitors
from tests import test_status
from tests.test_overview import page, text_of, tiles
from tests.test_status import Cluster, read

cluster = test_status.cluster  # the recording, behind both APIs, for every test here


def groups(text: str) -> dict[str, str]:
    """Each namespace group's header on the Monitors tab, as text."""
    found = re.findall(
        r'<h2 class="group">\s*<span class="mono">(.*?)</span>(.*?)</h2>', text, re.S
    )
    return {name: text_of(body) for name, body in found}


def pill(text: str, namespace: str) -> str:
    """The class of the pods' pill in a namespace's group header."""
    header = text[text.index(f'<h2 class="group">\n<span class="mono">{namespace}</span>') :]
    (tone,) = re.findall(r'<span class="pill (\w+)">pods', header.split("</h2>")[0])
    return tone  # type: ignore[no-any-return]


def answer(cluster: Cluster, key: str, namespace: str, value: int) -> None:
    """Edit the recording: `key`'s query answers `value` for the namespace."""
    result: list[dict[str, Any]] = cluster.bodies["replicas"][key]["data"]["result"]
    result[:] = [s for s in result if s["metric"]["namespace"] != namespace]
    result.append({"metric": {"namespace": namespace}, "value": [1794900852.399, str(value)]})


def test_full_strength_counts_every_namespace_s_pods() -> None:
    replicas = read()["replicas"]
    assert replicas["aipostit-prod"] == {"ready": 4, "desired": 4, "short": 0, "none": 0}
    assert {ns: (c["ready"], c["desired"]) for ns, c in replicas.items()} == {
        ns: (c, c)
        for ns, c in {
            "aipostit-prod": 4,
            "aipostit-staging": 3,
            "argo-rollouts": 1,
            "argocd": 5,
            "backups": 1,
            "cert-manager": 3,
            "cnpg-system": 2,
            "deploybot-ops": 1,
            "envoy-gateway-system": 2,
            "event-exporter": 1,
            "external-secrets": 3,
            "kube-system": 3,
            "kyverno": 3,
            "local-path-storage": 1,
            "monitoring": 7,
            "servicemonitor-ops": 1,
        }.items()
    }
    assert tiles(page())["argocd"][::2] == (
        "ok",
        "argocd Healthy Platform · 6/6 up · pods 5/5 ready · 0 alerts",
    )
    text = page("/monitors")
    assert groups(text)["argocd"] == "Platform pods 5/5 ready 4 monitors"
    assert pill(text, "argocd") == "ok"


def test_a_namespace_without_deployments_or_statefulsets_says_nothing_about_pods(
    cluster: Cluster,
) -> None:
    cluster.bodies["replicas"]["ready"]["data"]["result"] = []
    cluster.bodies["replicas"]["desired"]["data"]["result"] = []
    assert tiles(page())["argocd"][::2] == ("ok", "argocd Healthy Platform · 6/6 up · 0 alerts")
    assert groups(page("/monitors"))["argocd"] == "Platform 4 monitors"


def test_one_short_past_the_grace_period_turns_the_tile_alert(cluster: Cluster) -> None:
    answer(cluster, "ready", "argocd", 4)
    answer(cluster, "short", "argocd", 1)
    assert tiles(page())["argocd"][::2] == (
        "warn",
        "argocd Alert Platform · 6/6 up · pods 4/5 ready · 0 alerts",
    )
    text = page("/monitors")
    assert groups(text)["argocd"] == "Platform pods 4/5 ready · Alert 4 monitors"
    assert pill(text, "argocd") == "warn"


def test_a_brief_dip_inside_the_grace_period_shows_in_the_count_only(cluster: Cluster) -> None:
    answer(cluster, "ready", "argocd", 4)
    assert tiles(page())["argocd"][::2] == (
        "ok",
        "argocd Healthy Platform · 6/6 up · pods 4/5 ready · 0 alerts",
    )
    text = page("/monitors")
    assert groups(text)["argocd"] == "Platform pods 4/5 ready 4 monitors"
    assert pill(text, "argocd") == "ok"


def test_a_workload_with_none_ready_turns_the_tile_down_at_once(cluster: Cluster) -> None:
    """No grace: `short` hasn't answered it yet, `none` has."""
    answer(cluster, "ready", "aipostit-staging", 2)
    answer(cluster, "none", "aipostit-staging", 1)
    assert tiles(page())["aipostit-staging"][::2] == (
        "down",
        "aipostit-staging Down App · 1/1 up · pods 2/3 ready · 0 alerts · SLO -769%",
    )
    text = page("/monitors")
    assert groups(text)["aipostit-staging"] == "App pods 2/3 ready · Down 1 monitor"
    assert pill(text, "aipostit-staging") == "down"


def test_the_query_never_answering_leaves_the_pods_unknown_never_healthy(
    cluster: Cluster,
) -> None:
    cluster.down = {"replicas"}
    assert read()["replicas"] is None
    text = page()
    assert "Prometheus's query API has not answered yet: the pods ready are unknown." in text
    assert "connection refused" in text
    assert tiles(text)["argocd"][::2] == (
        "unknown",
        "argocd Unknown Platform · 6/6 up · pods unknown · 0 alerts",
    )
    assert tiles(text)["monitoring"][0] == "warn"  # its alert still marks it
    text = page("/monitors")
    assert groups(text)["argocd"] == "Platform pods unknown 4 monitors"
    assert pill(text, "argocd") == "unknown"


def test_the_query_failing_keeps_the_last_counts_and_says_since_when(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitors, "TTL", 0)
    page()
    cluster.down = {"replicas"}
    text = page()
    assert "Prometheus's query API has not answered since" in text
    assert "the pods ready below are what it said then." in text
    assert tiles(text)["argocd"][2].endswith("· pods 5/5 ready · 0 alerts")
