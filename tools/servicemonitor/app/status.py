"""The read model behind `/api/status`: what a green target doesn't say. For every monitor
of `monitors.read()`, the alerts firing on it (Prometheus `/api/v1/alerts`) and the error
budget its SLOs have left; and the page's summary line.

An alert goes to every monitor of its `namespace` label: the monitors that scrape a target
there (a monitor's own namespace only while it has no target, because a monitor in argocd
can scrape pods in kyverno). The monitor that also scrapes the alert's `job` there (and its
`instance`, when the alert has one: the front-door probes share one job) gets it as a "job"
match, the others as a "namespace" match. The job alone can't decide: an alert on a
kube-state-metrics series carries kube-state-metrics's job with the namespace of the
object it is about. An alert that reaches no monitor (no namespace, or one nothing is
scraped in) is `unattached`; the summary counts it too. Watchdog is left out everywhere: it
fires all the time by design (the dead man's switch), so it says nothing about the cluster.

The budget is the series the platform's SLO dashboard shows, recorded by the rules that
its `scripts/gen_slo_rules.py` generates (a test reads the name from the generator's
output). It carries `namespace` and `slo`, no job: every monitor that scrapes the
namespace gets its SLOs.

The replicas are each namespace's Deployments and StatefulSets, as kube-state-metrics sees
them (Jobs and CronJobs left out; no Argo Rollout exports its numbers): ready against
desired, the workloads short for the whole last minute, and those with none ready of one or
more. The minute is Prometheus's, not the app's: `min_over_time` of a workload's shortfall
over a 1m subquery, at the evaluation interval (15 s), is above 0 only if every point in it
was, so a rollout's brief dip shows in the count but isn't "short".

Like `monitors`: a query that fails keeps its last good answer and is marked stale; one
that has never answered leaves `alerts`, `slo` or `replicas` `null`, never "no alerts", a
full budget or every pod ready.
"""

import math
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from app import monitors

BUDGET = "slo:error_budget_remaining:ratio"  # 1 = untouched, below 0 = overspent
# The platform's ApplicationSet puts it on every app namespace, next to monitors.APP_LABEL.
ENV_LABEL = "easypeasyops.github.io/env"
# Fires all the time by design, so Alertmanager's receiver can tell alerting is alive: never
# a problem, so never counted or shown (history's alerts row leaves it out too).
ALWAYS_FIRING = "Watchdog"
_WORKLOAD = "namespace, deployment, statefulset"
_DESIRED = "(kube_deployment_spec_replicas or kube_statefulset_replicas)"
_READY = "(kube_deployment_status_replicas_ready or kube_statefulset_status_replicas_ready)"
_SHORTFALL = f"({_DESIRED} - on ({_WORKLOAD}) {_READY})"
REPLICAS = {  # each answers per namespace; a namespace it doesn't name has 0
    "ready": f"sum by (namespace) ({_READY})",
    "desired": f"sum by (namespace) ({_DESIRED})",
    "short": f"count by (namespace) (min_over_time({_SHORTFALL}[1m:]) > 0)",
    "none": f"count by (namespace) ({_DESIRED} >= 1 and on ({_WORKLOAD}) {_READY} == 0)",
}

_last_good: dict[str, tuple[str, Any]] = {}  # source -> (when it answered, what)
_cache: tuple[float, dict[str, Any]] | None = None  # the sources' state, for monitors.TTL


async def _get(path: str, **params: str) -> Any:
    async with httpx.AsyncClient(
        base_url=monitors.PROMETHEUS_URL, transport=monitors.transport
    ) as client:
        response = await client.get(path, params=params)
    return response.raise_for_status().json()["data"]


async def _alerts() -> list[dict[str, Any]]:
    return [
        {
            "name": alert["labels"]["alertname"],
            "severity": alert["labels"].get("severity"),
            "active_at": alert["activeAt"],
            "summary": alert["annotations"].get("summary"),
            "runbook_url": alert["annotations"].get("runbook_url"),
            "labels": alert["labels"],
            # Under Prometheus's own address: its alerts page, filtered to this alert's rule.
            "prometheus_path": f"/alerts?search={quote(alert['labels']['alertname'])}",
        }
        for alert in (await _get("/api/v1/alerts"))["alerts"]
        if alert["state"] == "firing"  # not pending: nobody has been told yet
        and alert["labels"]["alertname"] != ALWAYS_FIRING
    ]


async def _budgets() -> list[dict[str, Any]]:
    budgets = []
    for series in (await _get("/api/v1/query", query=BUDGET))["result"]:
        remaining = float(series["value"][1])
        budgets.append(
            {
                "namespace": series["metric"].get("namespace"),
                "slo": series["metric"]["slo"],
                # NaN: no request in the whole window, so nothing to divide. Not a budget.
                "remaining": remaining if math.isfinite(remaining) else None,
            }
        )
    return sorted(budgets, key=lambda b: (str(b["namespace"]), b["slo"]))


async def _replicas() -> dict[str, dict[str, int]]:
    """Per namespace with a Deployment or a StatefulSet: `ready`, `desired`, `short` and
    `none` (workloads)."""
    found: dict[str, dict[str, int]] = {}
    for key, query in REPLICAS.items():
        for series in (await _get("/api/v1/query", query=query))["result"]:
            counts = found.setdefault(series["metric"]["namespace"], dict.fromkeys(REPLICAS, 0))
            counts[key] = int(float(series["value"][1]))
    return found


async def _sources() -> dict[str, Any]:
    global _cache
    if _cache and time.monotonic() - _cache[0] < monitors.TTL:
        return _cache[1]
    sources = {}
    for source, fetch in (("alerts", _alerts), ("slo", _budgets), ("replicas", _replicas)):
        error = None
        try:
            _last_good[source] = (datetime.now(UTC).isoformat(timespec="seconds"), await fetch())
        except httpx.HTTPError as exc:
            error = str(exc) or type(exc).__name__
        as_of = _last_good[source][0] if source in _last_good else None
        sources[source] = {"stale": error is not None, "as_of": as_of, "error": error}
    _cache = (time.monotonic(), sources)
    return sources


def _job_monitors(alert: dict[str, Any], scraped: dict[str, list[dict[str, str]]]) -> set[str]:
    """The monitors that scrape the alert's job in its namespace: the one that scrapes its
    instance, if any does."""
    labels = alert["labels"]
    if "job" not in labels:
        return set()
    for keys in (("namespace", "job", "instance"), ("namespace", "job")):
        wanted = {key: labels[key] for key in keys if key in labels}
        found = {
            monitor
            for monitor, targets in scraped.items()
            if any(wanted.items() <= target.items() for target in targets)
        }
        if found:
            return found
    return set()


def _lowest(budgets: list[dict[str, Any]]) -> dict[str, Any] | None:
    known = [b for b in budgets if b["remaining"] is not None]
    return min(known, key=lambda b: b["remaining"], default=None)


def _slo(
    budgets: list[dict[str, Any]], covers: set[str], labels: dict[str, dict[str, str]]
) -> dict[str, Any]:
    mine = [b for b in budgets if b["namespace"] in covers]
    reason = None
    if not mine:
        reason = "no SLO"
        apps = {labels.get(ns, {}).get(monitors.APP_LABEL) for ns in covers} - {None}
        for budget in budgets:
            # Another environment of the same app has a budget, so its rules exist, and they
            # cover every environment (cluster-wide, by namespace: docs/DESIGN.md, SLO rules):
            # this one just has no requests to count.
            other = labels.get(budget["namespace"], {})
            if other.get(monitors.APP_LABEL) in apps and ENV_LABEL in other:
                reason = "no requests in the SLO's window"
                break
    return {"budgets": mine, "lowest": _lowest(mine), "reason": reason}


async def read() -> dict[str, Any]:
    model = await monitors.read()
    sources = await _sources()
    alerts = _last_good["alerts"][1] if "alerts" in _last_good else None
    budgets = _last_good["slo"][1] if "slo" in _last_good else None
    replicas = _last_good["replicas"][1] if "replicas" in _last_good else None
    # What the monitors' model leaves out: each namespace's labels, each target's labels.
    labels = (monitors.last_good("kubernetes") or {}).get("labels", {})
    targets = monitors.last_good("prometheus")
    scraped: dict[str, list[dict[str, str]]] = {}
    for target in targets or []:
        monitor = "/".join(target["scrapePool"].split("/")[:3])
        scraped.setdefault(monitor, []).append(target["labels"])
    by_job = [_job_monitors(alert, scraped) for alert in alerts or []]

    attached: set[int] = set()
    namespaces = None
    if model["namespaces"] is not None:
        namespaces = []
        for namespace in model["namespaces"]:
            rows = []
            for row in namespace["monitors"]:
                kind = row["kind"]
                monitor = f"{kind[0].lower()}{kind[1:]}/{namespace['name']}/{row['name']}"
                covers = {t["namespace"] for t in scraped.get(monitor, []) if "namespace" in t} or {
                    namespace["name"]
                }
                firing = []
                for i, alert in enumerate(alerts or []):
                    if monitor in by_job[i]:
                        firing.append({**alert, "match": "job"})
                    elif alert["labels"].get("namespace") in covers:
                        firing.append({**alert, "match": "namespace"})
                    else:
                        continue
                    attached.add(i)
                rows.append(
                    {
                        "kind": kind,
                        "name": row["name"],
                        "scrapes": sorted(covers),  # the namespaces its targets are in
                        "alerts": None if alerts is None else firing,
                        "slo": None if budgets is None else _slo(budgets, covers, labels),
                    }
                )
            namespaces.append({"name": namespace["name"], "monitors": rows})

    def count(health: str) -> int | None:
        return None if targets is None else sum(t["health"] == health for t in targets)

    return {
        "sources": {**model["sources"], **sources},
        "summary": {
            "monitors": None if namespaces is None else sum(len(n["monitors"]) for n in namespaces),
            "targets_up": count("up"),
            "targets_down": count("down"),
            "alerts_firing": None if alerts is None else len(alerts),
            "lowest_budget": None if budgets is None else _lowest(budgets),
        },
        "namespaces": namespaces,
        "replicas": replicas,
        "unattached": None
        if alerts is None or namespaces is None
        else [alert for i, alert in enumerate(alerts) if i not in attached],
    }
