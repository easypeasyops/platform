"""The Overview's last 24 hours: per hour, how many targets were down and how many alerts
were firing, from two Prometheus range queries with a 1 h step.

Each hour is the worst moment in it (`max_over_time` over a subquery at Prometheus's
evaluation interval), not one sample on the hour: a 20-minute outage between two steps
is in its hour's cell.

`count()` of nothing is nothing, not 0: an hour without a point could be "none down" or
"Prometheus wasn't running" (the laptop asleep, the cluster stopped). `or count(up) * 0`
makes it 0 while anything was scraped in that hour; an hour still without a point is
"no data". The alerts row counts every firing alert, Watchdog too, like the summary line.

Cached for a minute: it's history, not live state. A query that fails leaves its row
without cells, with the error: never 24 zeros.
"""

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from app import monitors

HOUR = 3600
HOURS = 24
TTL = 60.0
SCRAPING = "count(up) * 0"  # 0, while Prometheus scrapes anything
ROWS = {
    "down": ("Targets down", ("target down", "targets down"), "count(up == 0)"),
    "alerts": (
        "Alerts firing",
        ("alert firing", "alerts firing"),
        'count(ALERTS{alertstate="firing"})',
    ),
}
QUERIES = {key: f"max_over_time(({count} or {SCRAPING})[1h:])" for key, (*_, count) in ROWS.items()}

_cache: tuple[float, list[dict[str, Any]]] | None = None


def _iso(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def _cells(key: str, values: list[list[Any]], end: int) -> list[dict[str, Any]]:
    """24 hours, oldest first. The point at `t` is the hour up to `t`; the last hour is
    the one in progress."""
    found = {int(t): int(float(value)) for t, value in values}
    one, many = ROWS[key][1]
    cells = []
    for hour in range(end - (HOURS - 1) * HOUR, end + 1, HOUR):
        value = found.get(hour)
        if value is None:
            text = "no data: Prometheus wasn't running"
        elif value == 0:
            text = f"no {one}"
        else:
            text = f"up to {value} {one if value == 1 else many}"
        cells.append(
            {
                "start": _iso(hour - HOUR),
                "end": None if hour == end else _iso(hour),  # None: now
                "value": value,
                "level": "none" if value is None else str(min(value, 2)),
                "text": text,
            }
        )
    return cells


async def _row(client: httpx.AsyncClient, key: str, end: int) -> dict[str, Any]:
    params = {"start": end - (HOURS - 1) * HOUR, "end": end, "step": HOUR}
    row: dict[str, Any] = {"key": key, "name": ROWS[key][0], "cells": None, "error": None}
    try:
        response = await client.get("/api/v1/query_range", params={"query": QUERIES[key], **params})
        result = response.raise_for_status().json()["data"]["result"]
    except httpx.HTTPError as exc:
        row["error"] = str(exc) or type(exc).__name__
        return row
    row["cells"] = _cells(key, result[0]["values"] if result else [], end)
    return row


async def read(now: datetime) -> list[dict[str, Any]]:
    """One row per query: its name, and its cells or why there are none (`error`)."""
    global _cache
    if _cache and time.monotonic() - _cache[0] < TTL:
        return _cache[1]
    end = (int(now.timestamp()) // HOUR + 1) * HOUR  # the end of the hour in progress
    async with httpx.AsyncClient(
        base_url=monitors.PROMETHEUS_URL, transport=monitors.transport
    ) as client:
        rows = await asyncio.gather(*(_row(client, key, end) for key in QUERIES))
    if not any(row["error"] for row in rows):  # a failure is retried on the next load
        _cache = (time.monotonic(), rows)
    return rows
