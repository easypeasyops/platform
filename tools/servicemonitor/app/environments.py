"""The Overview's environments: for each app, what each of its environments runs (the
digest of its main container's image) and its Argo CD Application's sync and health, from
two Prometheus queries and the namespaces' labels the monitors' read already has.

An app's environments are the namespaces with both the platform's app and env labels
(app/status.py's ENV_LABEL). The main container is the service chart's `app` container, in
the Deployment's or the Rollout's pods; a pod not started yet has no digest and is left
out. kube-state-metrics's `image_id` is the digest the node pulled.

Marks: an environment running two digests at once (a rollout or a canary in progress), and
prod not on staging's digests (prod behind, or a promote in flight). An unknown digest
(the query failed, or no pod runs) never reads as matching. Like app/components.py, there
is no last good answer: a query that fails leaves what it says unknown, with the error.
"""

import asyncio
import time
from typing import Any

import httpx

from app import monitors, status

IMAGES = 'count by (namespace, image_id) (kube_pod_container_info{container="app", image_id!=""})'
APPLICATIONS = "max by (name, dest_namespace, sync_status, health_status) (argocd_app_info)"
ORDER = {"staging": 0, "prod": 1}  # then the others (a tool's one env), by name

transport: httpx.AsyncBaseTransport | None = None  # the tests' stand-in for the network

_cache: tuple[float, dict[str, Any]] | None = None


async def _query(client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    response = await client.get("/api/v1/query", params={"query": query})
    result: list[dict[str, Any]] = response.raise_for_status().json()["data"]["result"]
    return result


def _short(image_id: str) -> str:
    return "sha256:" + image_id.rsplit("sha256:", 1)[-1][:12]


def _tone(application: dict[str, str]) -> str:
    if application["health"] in ("Degraded", "Missing"):
        return "down"
    if application["sync"] == "Synced" and application["health"] == "Healthy":
        return "ok"
    return "warn"  # OutOfSync, Progressing, Suspended, Unknown


def table(
    labels: dict[str, dict[str, str]] | None,
    images: list[dict[str, Any]] | None,
    applications: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """A row per app, a cell per environment. `labels` is each namespace's (None: the
    Kubernetes API never answered); `images` and `applications` the two queries' series
    (None: the query failed)."""
    if labels is None:
        return None
    apps: dict[str, list[tuple[str, str]]] = {}
    for namespace, ns_labels in labels.items():
        if monitors.APP_LABEL in ns_labels and status.ENV_LABEL in ns_labels:
            apps.setdefault(ns_labels[monitors.APP_LABEL], []).append(
                (ns_labels[status.ENV_LABEL], namespace)
            )
    rows = []
    for app, envs in sorted(apps.items()):
        cells: dict[str, dict[str, Any]] = {}
        for env, namespace in sorted(envs, key=lambda e: (ORDER.get(e[0], 2), e[0])):
            digests = None
            if images is not None:
                digests = sorted(
                    {
                        s["metric"]["image_id"]: int(float(s["value"][1]))
                        for s in images
                        if s["metric"]["namespace"] == namespace
                    }.items()
                )
            apps_here = None
            if applications is not None:
                apps_here = [
                    {
                        "name": s["metric"]["name"],
                        "sync": s["metric"].get("sync_status", "Unknown"),
                        "health": s["metric"].get("health_status", "Unknown"),
                    }
                    for s in applications
                    if s["metric"].get("dest_namespace") == namespace
                ]
                for application in apps_here:
                    application["tone"] = _tone(application)
            cells[env] = {
                "env": env,
                "namespace": namespace,
                # None: unknown. Empty: no pod of the main container runs.
                "digests": None
                if digests is None
                else [
                    {"id": image_id, "short": _short(image_id), "pods": pods}
                    for image_id, pods in digests
                ],
                "rolling": digests is not None and len(digests) > 1,
                "applications": apps_here,
            }
        staging, prod = cells.get("staging"), cells.get("prod")
        prod_vs_staging = None  # None: the app has no staging and prod to compare
        if staging and prod:
            prod_vs_staging = "unknown"  # a side's digest unknown, or no pod runs
            if staging["digests"] and prod["digests"]:
                same = {d["id"] for d in prod["digests"]} == {d["id"] for d in staging["digests"]}
                prod_vs_staging = "same" if same else "differs"
        rows.append({"app": app, "cells": list(cells.values()), "prod_vs_staging": prod_vs_staging})
    return rows


async def read() -> dict[str, Any]:
    """`rows` (None: which apps there are is unknown) and each query's `errors`. Cached
    for monitors.TTL when both queries answer."""
    global _cache
    if _cache and time.monotonic() - _cache[0] < monitors.TTL:
        return _cache[1]
    async with httpx.AsyncClient(base_url=monitors.PROMETHEUS_URL, transport=transport) as client:
        answers = await asyncio.gather(
            _query(client, IMAGES), _query(client, APPLICATIONS), return_exceptions=True
        )
    errors = []
    found: list[list[dict[str, Any]] | None] = []
    for name, answer in zip(("images", "Applications"), answers, strict=True):
        if isinstance(answer, httpx.HTTPError):
            errors.append(f"{name}: {str(answer) or type(answer).__name__}")
            found.append(None)
        elif isinstance(answer, BaseException):
            raise answer
        else:
            found.append(answer)
    labels = (monitors.last_good("kubernetes") or {}).get("labels")
    view = {"rows": table(labels, *found), "errors": errors}
    if not errors:
        _cache = (time.monotonic(), view)
    return view
