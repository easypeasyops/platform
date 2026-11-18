"""The Overview's last 24 hours over range queries recorded from the laptop cluster
(tests/fixtures/history.json, the down row's query at 20:09 local on 2026-11-16, the alerts
row's over the same range at 22:05, once Watchdog was left out): the cluster
started 23 h before, ran for 3 hours, was stopped for 11 and has run since, with targets
down three hours before the end. `error` is a range query Prometheus refused, as it
answered. The deploys row's query over the same range is tests/fixtures/deploys.json,
recorded at 21:45 local on 2026-11-17 (the hour then in progress was over): the series
with a sync in the range, and argo-rollouts, whose counter didn't move."""

import asyncio
import html
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import history, main, monitors
from tests import test_status
from tests.test_overview import text_of
from tests.test_status import Cluster

cluster = test_status.cluster  # the recording, behind both APIs, for every test here
client = TestClient(main.app)
RECORDED = datetime.fromtimestamp(1794888541, UTC)  # 20:09:01 local
END = 1794891600  # the end of the hour in progress then: 21:00 local


def read() -> dict[str, dict[str, Any]]:
    return {row["key"]: row for row in asyncio.run(history.read(RECORDED))}


@pytest.fixture
def overview(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Overview tab, loaded when the recording was made."""
    monkeypatch.setattr(main, "datetime", SimpleNamespace(now=lambda tz: RECORDED))


def lanes(text: str) -> dict[str, tuple[list[str], str]]:
    """Each row of the strip: what each cell says, and the count at its end."""
    found = re.findall(
        r'<div class="lane">\n<span>(.*?)</span>\n(.*?)<b>(.*?)</b>',
        text[text.index("Last 24") :],
        re.S,
    )
    return {
        name: ([text_of(li) for li in re.findall(r"<li .*?</li>", body)] or [text_of(body)], now)
        for name, body, now in found
    }


def test_three_range_queries_of_24_hours_ending_with_the_hour_in_progress(
    cluster: Cluster,
) -> None:
    read()
    sent = [r.url.params for r in cluster.requests]
    syncs = 'argocd_app_sync_total{dry_run="false"}'
    assert [p["query"] for p in sent] == [
        "max_over_time((count(up == 0) or count(up) * 0)[1h:])",
        'max_over_time((count(ALERTS{alertstate="firing", alertname!="Watchdog"}) or count(up) * 0)'
        "[1h:])",
        f"sum by (name, phase) (clamp_min(max_over_time({syncs}[1h]) - last_over_time({syncs}[1d]"
        f" offset 1h), 0) or max_over_time({syncs}[1h])) or count(count_over_time(up[1h])) * 0",
    ]
    assert {(p["start"], p["end"], p["step"]) for p in sent} == {
        (str(END - 23 * 3600), str(END), "3600")
    }


def test_an_hour_with_a_target_down_is_its_worst_moment_and_the_last_is_now() -> None:
    cells = read()["down"]["cells"]
    assert len(cells) == 24
    assert [cell["value"] for cell in cells] == [28, 0, 0] + [None] * 11 + [2, 1, 5] + [0] * 7
    assert cells[16] == {
        "start": "2026-11-16T21:00:00+00:00",  # 1 PM local
        "end": "2026-11-16T22:00:00+00:00",
        "value": 5,
        "level": "2",
        "text": "up to 5 targets down",
    }
    assert cells[15]["text"] == "up to 1 target down"
    assert (cells[-1]["start"], cells[-1]["end"]) == ("2026-11-17T04:00:00+00:00", None)
    assert (cells[-1]["text"], cells[-1]["level"]) == ("no target down", "0")
    # The alerts row leaves out Watchdog, which always fires.
    assert 'alertname!="Watchdog"' in history.QUERIES["alerts"]
    assert min(c["value"] for c in read()["alerts"]["cells"] if c["value"] is not None) == 2


def test_an_hour_without_data_is_no_data_not_zero() -> None:
    cells = read()["down"]["cells"]
    assert {(c["value"], c["level"], c["text"]) for c in cells[3:14]} == {
        (None, "none", "no data: Prometheus wasn't running")
    }


def test_a_failed_query_leaves_its_row_unknown_with_the_error_and_is_retried(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = cluster.bodies["history"]["error"]

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("query") == history.QUERIES["down"]:
            cluster.requests.append(request)
            return httpx.Response(refused["status_code"], json=refused["body"])
        return cluster(request)

    monkeypatch.setattr(monitors, "transport", httpx.MockTransport(answer))
    rows = read()
    assert rows["down"]["cells"] is None
    assert rows["down"]["error"].startswith("Client error '400 Bad Request' for url ")
    assert len(rows["alerts"]["cells"]) == 24  # the other rows stand
    assert len(rows["deploys"]["cells"]) == 24
    read()
    assert len(cluster.requests) == 6  # not cached: the next load asks again


def test_the_strip_says_each_hour_and_its_value_and_ends_with_the_summary_s_count(
    overview: None,
) -> None:
    text = html.unescape(client.get("/").text)
    rows = lanes(text)
    assert list(rows) == ["Deploys", "Targets down", "Alerts firing"]
    down, now = rows["Targets down"]
    assert now == "0 now"  # the summary line's: every target up
    assert rows["Alerts firing"][1] == "2 now"  # the summary line's 2 alerts firing
    assert len(down) == 24
    assert down[0] == "05:00 UTC – 06:00 UTC : up to 28 targets down"  # until the browser's time
    assert down[5] == "10:00 UTC – 11:00 UTC : no data: Prometheus wasn't running"
    assert down[-1] == "04:00 UTC –now: no target down"
    # Seen: the number in the cell (hidden from a screen reader, which reads the text);
    # no data is dashed, not a 0.
    assert '<li class="cell h-down l2"><span class="sr">' in text
    assert re.search(
        r'<li class="cell h-down lnone">.*?<span aria-hidden="true"></span></li>', text
    )
    assert re.search(r'<li class="cell h-down l2">.*?<span aria-hidden="true">28</span></li>', text)
    assert text.count("data-hour>") == 3 * (2 * 24 - 1)  # every hour but "now", in local time


def test_a_query_that_never_answers_shows_unknown_never_zeros(
    cluster: Cluster, overview: None
) -> None:
    cluster.down = {"history"}
    text = html.unescape(client.get("/").text)
    unknown = ["unknown: Prometheus's query API did not answer. connection refused"]
    assert lanes(text) == {
        "Deploys": (unknown, "? this hour"),
        "Targets down": (unknown, "0 now"),
        "Alerts firing": (unknown, "2 now"),
    }
    assert '<li class="cell' not in text


def test_the_strip_is_cached_for_a_minute(cluster: Cluster) -> None:
    read()
    read()
    assert len(cluster.requests) == 3
    assert history._cache is not None
    history._cache = (history._cache[0] - history.TTL, history._cache[1])  # a minute later
    read()
    assert len(cluster.requests) == 6


def deploys() -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = read()["deploys"]["cells"]
    return cells


def test_an_hour_with_one_sync_names_its_application() -> None:
    cell = deploys()[17]
    assert cell == {
        "start": "2026-11-16T22:00:00+00:00",  # gateway's sync at 22:23:02Z
        "end": "2026-11-16T23:00:00+00:00",
        "value": 1,
        "level": "1",
        "text": "1 sync: gateway",
        "syncs": [{"app": "gateway", "phase": "Succeeded", "count": 1}],
    }
    # The hour in progress: the platform merge at 04:07Z, and what it synced.
    assert (deploys()[-1]["end"], deploys()[-1]["text"]) == (
        None,
        "3 syncs: apps, platform, servicemonitor-ops",
    )


def test_several_applications_in_one_hour_and_a_failed_sync_marked_as_such() -> None:
    cell = deploys()[0]
    assert cell["text"] == (
        "8 syncs: aipostit-prod, aipostit-staging failed (Error), aipostit-staging,"
        " envoy-gateway ×2, external-secrets, kyverno, monitoring"
    )
    assert (cell["value"], cell["level"]) == (8, "2")  # a failed one among them
    assert [c["level"] for c in deploys()[14:18]] == ["1"] * 4  # all succeeded


def test_hours_without_syncs_are_no_deploys_and_a_counter_that_did_not_move_is_none() -> None:
    cells = deploys()
    assert [i for i, c in enumerate(cells) if c["value"] == 0] == [2, 18, 19, 21, 22]
    assert {
        (c["level"], c["text"], tuple(c["syncs"])) for i, c in enumerate(cells) if i in (2, 18)
    } == {("0", "no deploys", ())}
    # argo-rollouts is in the recording at 0 every hour: synced before the range, not in it.
    assert not any(s["app"] == "argo-rollouts" for c in cells for s in c["syncs"])
    # A no-op resync can't be told from a sync by the counter: a dry run is left out by the
    # query, and an automated sync only runs on an OutOfSync Application (app/history.py).
    assert 'dry_run="false"' in history.QUERIES["deploys"]


def test_an_hour_without_data_is_no_data_never_no_deploys() -> None:
    assert {(c["value"], c["level"], c["text"]) for c in deploys()[3:14]} == {
        (None, "none", "no data: Prometheus wasn't running")
    }


def test_a_failed_deploys_query_leaves_its_row_unknown_and_the_others_stand(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = cluster.bodies["history"]["error"]

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("query") == history.QUERIES["deploys"]:
            return httpx.Response(refused["status_code"], json=refused["body"])
        return cluster(request)

    monkeypatch.setattr(monitors, "transport", httpx.MockTransport(answer))
    rows = read()
    assert (rows["deploys"]["cells"], rows["deploys"]["name"]) == (None, "Deploys")
    assert rows["deploys"]["error"].startswith("Client error '400 Bad Request' for url ")
    assert len(rows["down"]["cells"]) == len(rows["alerts"]["cells"]) == 24
    assert history._cache is None  # retried on the next load


def test_the_deploys_lane_can_be_focused_where_it_has_syncs_and_says_which(
    overview: None,
) -> None:
    text = html.unescape(client.get("/").text)
    cells, now = lanes(text)["Deploys"]
    assert now == "3 this hour"
    assert cells[17] == "22:00 UTC – 23:00 UTC : 1 sync: gateway"
    assert cells[5] == "10:00 UTC – 11:00 UTC : no data: Prometheus wasn't running"
    assert text.count('<li class="cell h-deploys l2" tabindex="0">') == 1
    assert text.count('<li class="cell h-deploys l1" tabindex="0">') == 7
    assert text.count('<li class="cell h-deploys l0">') == 5  # nothing to focus
    assert re.search(
        r'<li class="cell h-deploys l2" tabindex="0">.*?<span aria-hidden="true">8</span>', text
    )
