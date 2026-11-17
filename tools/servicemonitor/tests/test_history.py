"""The Overview's last 24 hours over range queries recorded from the laptop cluster
(tests/fixtures/history.json, both rows' queries at 20:09 local on 2026-11-16): the cluster
started 23 h before, ran for 3 hours, was stopped for 11 and has run since, with targets
down three hours before the end. `error` is a range query Prometheus refused, as it
answered."""

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


def test_two_range_queries_of_24_hours_ending_with_the_hour_in_progress(
    cluster: Cluster,
) -> None:
    read()
    sent = [r.url.params for r in cluster.requests]
    assert [p["query"] for p in sent] == [
        "max_over_time((count(up == 0) or count(up) * 0)[1h:])",
        'max_over_time((count(ALERTS{alertstate="firing"}) or count(up) * 0)[1h:])',
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
    # The alerts row counts Watchdog, which always fires: its quietest hour has 3.
    assert min(c["value"] for c in read()["alerts"]["cells"] if c["value"] is not None) == 3


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
    assert len(rows["alerts"]["cells"]) == 24  # the other row stands
    read()
    assert len(cluster.requests) == 4  # not cached: the next load asks again


def test_the_strip_says_each_hour_and_its_value_and_ends_with_the_summary_s_count(
    overview: None,
) -> None:
    text = html.unescape(client.get("/").text)
    rows = lanes(text)
    assert list(rows) == ["Targets down", "Alerts firing"]
    down, now = rows["Targets down"]
    assert now == "0 now"  # the summary line's: every target up
    assert rows["Alerts firing"][1] == "3 now"  # the summary line's 3 alerts firing
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
    assert text.count("data-hour>") == 2 * (2 * 24 - 1)  # every hour but "now", in local time


def test_a_query_that_never_answers_shows_unknown_never_zeros(
    cluster: Cluster, overview: None
) -> None:
    cluster.down = {"history"}
    text = html.unescape(client.get("/").text)
    unknown = ["unknown: Prometheus's query API did not answer. connection refused"]
    assert lanes(text) == {"Targets down": (unknown, "0 now"), "Alerts firing": (unknown, "3 now")}
    assert '<li class="cell' not in text


def test_the_strip_is_cached_for_a_minute(cluster: Cluster) -> None:
    read()
    read()
    assert len(cluster.requests) == 2
    assert history._cache is not None
    history._cache = (history._cache[0] - history.TTL, history._cache[1])  # a minute later
    read()
    assert len(cluster.requests) == 4
