"""The platform components of the Overview tab: which monitors, alerts, dashboard and key
signal belong to each, from `components.toml` beside this file (a config change, not a
code change, adds one), and each signal's numbers from Prometheus's query API.

The config is read and checked when the app starts: a mistake in it stops the app with
what is wrong and where. A query that fails, or answers more than one number, leaves its
component's signal unknown with the error; the others are unaffected. There is no last
good answer: a signal is never shown for a time it doesn't say.
"""

import asyncio
import re
import string
import time
import tomllib
from pathlib import Path
from typing import Any

import httpx

from app import monitors

CONFIG = Path(__file__).with_name("components.toml")
FIELDS = {"name", "monitors", "alerts", "dashboard", "queries", "signal", "empty"}
REQUIRED = FIELDS - {"dashboard", "empty"}
MONITOR = re.compile(rf"({'|'.join(monitors.KINDS)})/[^/\s]+/[^/\s]+")

_cache: tuple[float, list[dict[str, Any]]] | None = None


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v for v in value)


def _check(component: Any) -> str | None:
    """What is wrong with one component's entry, if anything."""
    if not isinstance(component, dict):
        return "is not a table"
    if missing := REQUIRED - component.keys():
        return f"has no {', '.join(sorted(missing))}"
    if unknown := component.keys() - FIELDS:
        return f"has unknown keys {', '.join(sorted(unknown))}"
    names = component["monitors"]
    if not names or not _strings(names) or not all(MONITOR.fullmatch(m) for m in names):
        return f"monitors must be a list of Kind/namespace/name, not {names!r}"
    if not _strings(component["alerts"]):
        return "alerts must be a list of alert names"
    queries = component["queries"]
    if not isinstance(queries, dict) or not queries or not _strings(list(queries.values())):
        return "queries must be a table of PromQL strings"
    if not all(
        isinstance(component.get(k, ""), str) for k in ("name", "signal", "dashboard", "empty")
    ):
        return "name, signal, dashboard and empty must be strings"
    try:
        used = {field for _, field, _, _ in string.Formatter().parse(component["signal"]) if field}
        if used == queries.keys():
            component["signal"].format(**dict.fromkeys(queries, 0.0))  # numbers it can show
    except (ValueError, IndexError) as exc:
        return f"signal is not a format string for the queries' numbers: {exc}"
    if used != queries.keys():
        return f"signal uses {sorted(used)}, the queries are {sorted(queries)}"
    return None


def load(path: Path = CONFIG) -> list[dict[str, Any]]:
    try:
        components = tomllib.loads(path.read_text()).get("component")
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    if not isinstance(components, list) or not components:
        raise ValueError(f"{path}: no [[component]]")
    for i, component in enumerate(components, 1):
        if problem := _check(component):
            name = component.get("name") if isinstance(component, dict) else None
            raise ValueError(f"{path}: component {i} ({name or 'no name'}) {problem}")
    return components


COMPONENTS = load()


async def _number(client: httpx.AsyncClient, name: str, query: str) -> float | None:
    """The query's one number; None when it answers no series."""
    try:
        response = await client.get("/api/v1/query", params={"query": query})
    except httpx.HTTPError as exc:
        raise ValueError(f"{name}: {str(exc) or type(exc).__name__}") from exc
    if response.is_error:  # Prometheus says why: a bad query, a timeout
        raise ValueError(f"{name}: Prometheus answered {response.status_code} {response.text}")
    result = response.json()["data"]["result"]
    if len(result) > 1:
        raise ValueError(f"{name}: {len(result)} series, not one")
    return float(result[0]["value"][1]) if result else None


async def _reading(client: httpx.AsyncClient, component: dict[str, Any]) -> dict[str, Any]:
    """The signal's text, or why it is unknown (`error`)."""
    queries = component["queries"]
    try:
        numbers = dict(
            zip(
                queries,
                await asyncio.gather(*(_number(client, n, q) for n, q in queries.items())),
                strict=True,
            )
        )
    except ValueError as exc:
        return {"text": None, "error": str(exc)}
    if None in numbers.values():
        if "empty" in component:
            return {"text": component["empty"], "error": None}
        nothing = ", ".join(name for name, n in numbers.items() if n is None)
        return {"text": None, "error": f"no series for {nothing}"}
    return {"text": component["signal"].format(**numbers), "error": None}


async def read() -> list[dict[str, Any]]:
    """Each component of COMPONENTS, in order, with its signal's `reading`: cached for
    monitors.TTL, like the other sources."""
    global _cache
    if _cache and time.monotonic() - _cache[0] < monitors.TTL:
        return _cache[1]
    async with httpx.AsyncClient(
        base_url=monitors.PROMETHEUS_URL, transport=monitors.transport
    ) as client:
        readings = await asyncio.gather(*(_reading(client, c) for c in COMPONENTS))
    components = [{**c, "reading": r} for c, r in zip(COMPONENTS, readings, strict=True)]
    _cache = (time.monotonic(), components)
    return components
