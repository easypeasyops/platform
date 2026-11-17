"""The Overview's Platform Radar (PT-60): the platform's weekly self-assessment on six
measures, read from docs/radar/scores.json on `main` (docs/radar/README.md), so a week
merged there shows here within the hour, without a release.

The newest week is drawn as a radar over the target ring at 4, with the week before it
dashed, and listed weakest first, each axis at target or below 4. The list is what a screen
reader gets; the drawing, the same numbers, is hidden from it.

A file that can't be fetched or read leaves the panel out, with one line saying why: never
a chart drawn from half a file. Cached for an hour; a failure for a minute, so a page that
refreshes every 15 s doesn't ask GitHub each time.
"""

import math
import os
import time
from typing import Any

import httpx

URL = os.environ.get(
    "RADAR_URL",
    "https://raw.githubusercontent.com/easypeasyops/platform/main/docs/radar/scores.json",
)
LINK = os.environ.get("RADAR_LINK_URL", "https://claude.ai/artifact/VemWi4hrxxuzhJf1L9rLuL")
TTL = 3600.0
RETRY = 60.0
TARGET = 4.0
TOP = 5.0
CX, CY, R = 200, 130, 80  # the drawing's centre and outer ring, in a 400 × 260 viewBox

transport: httpx.AsyncBaseTransport | None = None  # the tests' stand-in for the network
_cache: tuple[float, dict[str, Any]] | None = None  # (until when, the answer)


def _weeks(body: Any) -> list[tuple[str, str, dict[str, float]]]:
    """Each week, newest first: its Monday, the commit scored, and each axis's score in
    the file's order. Raises KeyError, TypeError or ValueError on anything else."""
    weeks: dict[str, tuple[str, dict[str, float]]] = {}
    for row in body["rows"]:
        score = row["score"]
        if isinstance(score, bool) or not isinstance(score, int | float) or not 0 <= score <= TOP:
            raise ValueError(f"{row['axis']}: score {score!r} is not between 0 and 5")
        weeks.setdefault(str(row["week"]), (str(row["commit"]), {}))[1][str(row["axis"])] = score
    found = [(week, *weeks[week]) for week in sorted(weeks, reverse=True)]
    if not found or len(found[0][2]) < 3:
        raise ValueError("no week with three axes or more")
    return found


def _direction(i: int, n: int) -> tuple[float, float]:
    angle = -math.pi / 2 + 2 * math.pi * i / n  # the first axis straight up, then clockwise
    return math.cos(angle), math.sin(angle)


def _point(i: int, n: int, score: float) -> tuple[float, float]:
    cos, sin = _direction(i, n)
    return CX + R * score / TOP * cos, CY + R * score / TOP * sin


def _shape(scores: list[float]) -> str:
    return " ".join(
        "{:.1f},{:.1f}".format(*_point(i, len(scores), s)) for i, s in enumerate(scores)
    )


def _label(i: int, n: int, name: str, score: float) -> dict[str, Any]:
    """An axis's name and score just outside the outer ring, on two lines when long."""
    x, y = _point(i, n, TOP + 0.75)
    cos, sin = _direction(i, n)
    lines = name.split(" ", 1) if len(name) > 14 else [name]
    height = 13 * (len(lines) - 1)
    if sin < -0.5:  # above the ring: the last line just over it
        first = y - height - 2
    elif sin > 0.5:  # below: the first line just under it
        first = y + 10
    else:  # beside: centred on the axis
        first = y - height / 2 + 4
    return {
        "x": f"{x:.1f}",
        "anchor": "middle" if abs(cos) < 0.1 else "start" if cos > 0 else "end",
        "lines": [{"y": f"{first + 13 * k:.1f}", "text": line} for k, line in enumerate(lines)],
        "score": f"{score:.1f}",
    }


def _view(weeks: list[tuple[str, str, dict[str, float]]]) -> dict[str, Any]:
    week, commit, scores = weeks[0]
    names, n = list(scores), len(scores)
    before = weeks[1] if len(weeks) > 1 else None
    # The week before is drawn only if it scored every axis this week has.
    last = before[2] if before and all(name in before[2] for name in names) else None
    return {
        "week": week,
        "commit": commit,
        "link": LINK,
        "average": f"{sum(scores.values()) / n:.2f}",
        "count": n,
        "before": before
        and {"week": before[0], "average": f"{sum(before[2].values()) / len(before[2]):.2f}"},
        "rings": [_shape([k] * n) for k in range(1, int(TOP) + 1)],
        "spokes": "".join(
            "M{} {}L{:.1f} {:.1f}".format(CX, CY, *_point(i, n, TOP)) for i in range(n)
        ),
        "now": _shape(list(scores.values())),
        "last": last and _shape([last[name] for name in names]),
        "labels": [_label(i, n, name, scores[name]) for i, name in enumerate(names)],
        "axes": [
            {
                "name": name,
                "score": f"{scores[name]:.1f}",
                "width": round(scores[name] / TOP * 100),
                "below": scores[name] < TARGET,
                "last": f"{before[2][name]:.1f}" if before and name in before[2] else None,
            }
            for name in sorted(names, key=lambda name: scores[name])  # weakest first
        ],
    }


async def read() -> dict[str, Any]:
    """`radar`: the panel, or None with `error` saying why there is none."""
    global _cache
    if _cache and time.monotonic() < _cache[0]:
        return _cache[1]
    try:
        async with httpx.AsyncClient(transport=transport, timeout=3.0) as client:
            body = (await client.get(URL)).raise_for_status().json()
        answer: dict[str, Any] = {"radar": _view(_weeks(body)), "error": None}
    except httpx.HTTPStatusError as exc:
        answer = {"radar": None, "error": f"GitHub answered {exc.response.status_code}"}
    except httpx.HTTPError as exc:
        answer = {
            "radar": None,
            "error": f"GitHub did not answer: {str(exc) or type(exc).__name__}",
        }
    except (KeyError, TypeError, ValueError) as exc:
        answer = {"radar": None, "error": f"not a scores file: {exc!r}"}
    _cache = (time.monotonic() + (RETRY if answer["error"] else TTL), answer)
    return answer
