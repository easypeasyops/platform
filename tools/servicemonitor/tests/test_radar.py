"""The Overview's Platform Radar over the scores file recorded from main
(tests/fixtures/scores.json: the week of 2026-11-16, its only week so far). A second week is
that one carried to the next Monday with one axis changed, here."""

import asyncio
import copy
import html
import re
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import radar
from app.main import app
from tests import test_status
from tests.conftest import GitHub
from tests.test_overview import text_of

cluster = test_status.cluster  # the recording, behind both APIs, for the page's other panels
client = TestClient(app)


def read() -> dict[str, Any]:
    return asyncio.run(radar.read())


def next_week(github: GitHub, **scores: float) -> None:
    """Adds the week of 2026-11-23: last week's rows, with these axes' scores changed."""
    rows = [copy.deepcopy(row) for row in github.body["rows"]]
    for row in rows:
        row.update(week="2026-11-23", commit="abc1234")
        row["score"] = scores.get(row["axis"], row["score"])
    github.body["rows"] += rows


def panel() -> str:
    text = html.unescape(client.get("/").text)
    return text[text.index('aria-labelledby="radar"') : text.index("Last 24 hours")]


def test_it_reads_main_on_github() -> None:
    read()
    assert radar.URL == (
        "https://raw.githubusercontent.com/easypeasyops/platform/main/docs/radar/scores.json"
    )


def test_the_first_week_is_drawn_with_nothing_to_compare(github: GitHub) -> None:
    view = read()["radar"]
    assert (view["week"], view["commit"], view["average"], view["before"]) == (
        "2026-11-16",
        "5fc1f2a",
        "3.67",
        None,
    )
    assert view["last"] is None
    assert len(view["rings"]) == 5  # 1 to 5; the 4th is the target
    # Supply-chain security, straight up at 4.5 of 5.
    assert view["now"].split()[0] == "200.0,58.0"
    assert [(a["name"], a["score"], a["below"]) for a in view["axes"]] == [
        ("Reliability and recovery", "2.5", True),
        ("Developer experience", "3.0", True),
        ("Observability", "4.0", False),
        ("Delivery safety", "4.0", False),
        ("Operability and automation", "4.0", False),
        ("Supply-chain security", "4.5", False),
    ]
    assert "first week, nothing to compare yet" in panel()


def test_the_newest_week_is_drawn_over_last_week_s_dashed_shape(github: GitHub) -> None:
    next_week(github, **{"Reliability and recovery": 3.5})
    view = read()["radar"]
    assert (view["week"], view["commit"], view["average"]) == ("2026-11-23", "abc1234", "3.83")
    assert view["before"] == {"week": "2026-11-16", "average": "3.67"}
    assert view["last"] != view["now"]
    assert [a["name"] for a in view["axes"]][:2] == [
        "Developer experience",
        "Reliability and recovery",
    ]
    assert view["axes"][1]["last"] == "2.5"
    text = panel()
    assert '<polygon class="last" points="' in text
    assert "Platform Radar · week of 2026-11-23 · commit abc1234" in text
    assert "week of 2026-11-16: 3.67 (grey dashed shape)" in text


def test_a_week_before_without_every_axis_is_not_drawn(github: GitHub) -> None:
    next_week(github)
    github.body["rows"] = github.body["rows"][1:]  # 2026-11-16 without Supply-chain security
    view = read()["radar"]
    assert view["before"]["week"] == "2026-11-16" and view["last"] is None
    assert view["axes"][-1]["last"] is None


def test_a_screen_reader_gets_the_list_not_the_drawing(github: GitHub) -> None:
    next_week(github, **{"Reliability and recovery": 3.5})
    text = panel()
    assert re.search(r'<svg [^>]*aria-hidden="true"', text)
    items = [text_of(li) for li in re.findall(r"<li>.*?</li>", text)]
    assert items[:2] == [
        "Developer experience 3.0 below 4 , the week before: 3.0",
        "Reliability and recovery 3.5 below 4 , the week before: 2.5",
    ]
    assert items[-1] == "Supply-chain security 4.5 at target , the week before: 4.5"
    assert 'href="https://claude.ai/artifact/VemWi4hrxxuzhJf1L9rLuL">Full radar ↗</a>' in text


def note() -> str:
    text = html.unescape(client.get("/").text)
    assert 'aria-labelledby="radar"' not in text and "<svg viewBox" not in text
    (line,) = re.findall(r'<p class="radar-off">(.*?)</p>', text)
    return text_of(line)


def test_a_missing_file_hides_the_panel_with_one_line(github: GitHub) -> None:
    github.status = 404
    assert note() == (
        "Platform Radar hidden: docs/radar/scores.json on main could not be read "
        "(GitHub answered 404)."
    )


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ("<html>rate limited</html>", "not a scores file: JSONDecodeError("),
        ({"rows": []}, "not a scores file: ValueError('no week with three axes or more')"),
        ({"weeks": []}, "not a scores file: KeyError('rows')"),
        ([1, 2], "not a scores file: TypeError("),
    ],
)
def test_an_unreadable_file_hides_the_panel_too(github: GitHub, body: Any, why: str) -> None:
    github.body = body
    assert why in note()


def test_a_score_out_of_range_is_unreadable_not_drawn(github: GitHub) -> None:
    github.body["rows"][0]["score"] = "high"
    assert read() == {
        "radar": None,
        "error": "not a scores file: ValueError(\"Supply-chain security: score 'high' is not "
        'between 0 and 5")',
    }


def test_github_not_answering_hides_the_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(radar, "transport", httpx.MockTransport(refuse))
    assert read()["error"] == "GitHub did not answer: connection refused"


def test_a_week_merged_to_main_shows_within_the_hour(github: GitHub) -> None:
    assert read()["radar"]["week"] == "2026-11-16"
    next_week(github)
    assert read()["radar"]["week"] == "2026-11-16"  # cached
    assert len(github.requests) == 1
    assert radar._cache is not None
    radar._cache = (radar._cache[0] - radar.TTL, radar._cache[1])  # an hour later
    assert read()["radar"]["week"] == "2026-11-23"
    assert len(github.requests) == 2


def test_a_failure_is_asked_again_after_a_minute(github: GitHub) -> None:
    github.status = 404
    read()
    read()
    assert len(github.requests) == 1  # not on every 15 s refresh
    assert radar._cache is not None
    radar._cache = (radar._cache[0] - radar.RETRY, radar._cache[1])
    github.status = 200
    assert read()["radar"]["week"] == "2026-11-16"
