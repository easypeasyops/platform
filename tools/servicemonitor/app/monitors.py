"""The read model behind `/api/monitors`: every ServiceMonitor, PodMonitor and Probe (the
Kubernetes API) joined with the targets Prometheus scrapes for it (`/api/v1/targets`),
grouped by the monitor's namespace, with the namespace's owner.

The join key is each target's `scrapePool`, which the Prometheus operator names after the
monitor: `serviceMonitor/<namespace>/<name>/<endpoint index>`, `podMonitor/...` the same,
`probe/<namespace>/<name>`. The `job` and `namespace` labels can't be the key: `namespace`
is where the scraped pod runs, not where the monitor lives, and one monitor can have
several jobs (one per Service) while several monitors share one (a Probe's `jobName`).

A source that fails keeps its last good answer and is marked stale. One that has never
answered leaves what depends on it `null`, never empty: no targets known is not "no targets".
"""

import asyncio
import os
import ssl
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

# Defaults for a pod on the platform's cluster. Outside it: `kubectl proxy`'s address, and
# Prometheus through the API server's service proxy.
KUBERNETES_URL = os.environ.get("KUBERNETES_URL", "https://kubernetes.default.svc")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://kps-prometheus.monitoring.svc:9090")
# The pod's ServiceAccount, mounted by the kubelet. Absent outside a cluster, where
# `kubectl proxy` does the authenticating.
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
# The platform's ApplicationSet puts it on every app namespace; one without it is platform.
APP_LABEL = "easypeasyops.github.io/app"
KINDS = {"ServiceMonitor": "servicemonitors", "PodMonitor": "podmonitors", "Probe": "probes"}
TTL = 15.0  # seconds: a page load doesn't hit both APIs every time

transport: httpx.AsyncBaseTransport | None = None  # the tests' stand-in for the network

_last_good: dict[str, tuple[str, Any]] = {}  # source -> (when it answered, what)
_cache: tuple[float, dict[str, Any]] | None = None


async def _kubernetes() -> dict[str, Any]:
    """Each namespace's owner, and every monitor as (kind, namespace, name)."""
    headers = {}
    verify: ssl.SSLContext | bool = True
    if SERVICE_ACCOUNT.exists():
        # Read each time: the kubelet rotates the token.
        headers["Authorization"] = f"Bearer {(SERVICE_ACCOUNT / 'token').read_text().strip()}"
        verify = ssl.create_default_context(cafile=SERVICE_ACCOUNT / "ca.crt")
    paths = ["/api/v1/namespaces", *(f"/apis/monitoring.coreos.com/v1/{p}" for p in KINDS.values())]
    async with httpx.AsyncClient(
        base_url=KUBERNETES_URL, headers=headers, verify=verify, transport=transport
    ) as client:
        responses = await asyncio.gather(*(client.get(path) for path in paths))
    namespaces, *monitors = (r.raise_for_status().json()["items"] for r in responses)
    return {
        "owners": {
            ns["metadata"]["name"]: "app"
            if APP_LABEL in ns["metadata"].get("labels", {})
            else "platform"
            for ns in namespaces
        },
        "monitors": sorted(
            (item["metadata"]["namespace"], item["metadata"]["name"], kind)
            for kind, items in zip(KINDS, monitors, strict=True)
            for item in items
        ),
    }


async def _prometheus() -> list[dict[str, Any]]:
    async with httpx.AsyncClient(base_url=PROMETHEUS_URL, transport=transport) as client:
        # Without state=active the answer also holds every dropped target: 2 MB, not 200 kB.
        response = await client.get("/api/v1/targets", params={"state": "active"})
    targets: list[dict[str, Any]] = response.raise_for_status().json()["data"]["activeTargets"]
    return targets


def _join(
    kubernetes: dict[str, Any] | None, targets: list[dict[str, Any]] | None
) -> dict[str, Any]:
    by_monitor: dict[str, list[dict[str, Any]]] = {}
    for target in targets or []:
        monitor = "/".join(target["scrapePool"].split("/")[:3])
        by_monitor.setdefault(monitor, []).append(
            {
                "scrape_pool": target["scrapePool"],
                "endpoint": target["scrapeUrl"],
                "health": target["health"],
                "last_scrape": target["lastScrape"],
                "duration_seconds": target["lastScrapeDuration"],
                "last_error": target["lastError"],
            }
        )
    if kubernetes is None:
        return {"namespaces": None, "unclaimed": None}
    namespaces: dict[str, dict[str, Any]] = {}
    for namespace, name, kind in kubernetes["monitors"]:
        claimed = by_monitor.pop(f"{kind[0].lower()}{kind[1:]}/{namespace}/{name}", [])
        namespaces.setdefault(
            namespace,
            {
                "name": namespace,
                "owner": kubernetes["owners"].get(namespace, "platform"),
                "monitors": [],
            },
        )["monitors"].append(
            {"kind": kind, "name": name, "targets": None if targets is None else claimed}
        )
    return {
        "namespaces": list(namespaces.values()),
        # Targets of a scrape pool that is no monitor's: a scrape config of Prometheus's own.
        "unclaimed": None if targets is None else [t for ts in by_monitor.values() for t in ts],
    }


async def read() -> dict[str, Any]:
    global _cache
    if _cache and time.monotonic() - _cache[0] < TTL:
        return _cache[1]
    sources = {}
    for source, fetch in (("kubernetes", _kubernetes), ("prometheus", _prometheus)):
        error = None
        try:
            _last_good[source] = (datetime.now(UTC).isoformat(timespec="seconds"), await fetch())
        except httpx.HTTPError as exc:
            error = str(exc) or type(exc).__name__
        as_of = _last_good[source][0] if source in _last_good else None
        sources[source] = {"stale": error is not None, "as_of": as_of, "error": error}
    data = {name: _last_good[name][1] if name in _last_good else None for name in sources}
    model = {"sources": sources, **_join(data["kubernetes"], data["prometheus"])}
    _cache = (time.monotonic(), model)
    return model
