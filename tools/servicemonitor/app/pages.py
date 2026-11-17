"""The Monitors tab: the read model (app/monitors.py) shaped for templates/monitors.html.
Filtering is done here, from the URL, so a filtered link shows the same rows to whoever
opens it.

A monitor has a problem when it has a target that isn't up, no target at all, or targets
nobody knows (Prometheus never answered): unknown is never shown as healthy.
"""

import json
import os
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

# Where a browser reaches the tools: the front door's `tools` listener on the laptop.
GRAFANA = os.environ.get("GRAFANA_LINK_URL", "http://grafana.localhost:8090")
PROMETHEUS = os.environ.get("PROMETHEUS_LINK_URL", "http://prometheus.localhost:8090")
ARGOCD = os.environ.get("ARGOCD_LINK_URL", "http://argocd.localhost:8090")

SHOW = {"all": "All", "problems": "Problems only", "apps": "Apps", "platform": "Platform"}
# Each source of the read model: what the page calls it, and what is unknown without it.
SOURCES = {
    "kubernetes": ("The Kubernetes API", "the monitors"),
    "prometheus": ("Prometheus", "the targets"),
}


def _ago(then: str, now: datetime) -> str:
    when = datetime.fromisoformat(then)
    if when.year == 1:  # Prometheus's zero time: not scraped yet
        return "never"
    seconds = max(int((now - when).total_seconds()), 0)
    if seconds < 120:
        return f"{seconds} s ago"
    if seconds < 7200:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h ago"


def _logs(namespaces: list[str]) -> str:
    """Grafana's Explore on VictoriaLogs (the platform's RUNBOOK, Logs): the last hour of
    the namespaces' lines."""
    datasource = {"type": "victoriametrics-logs-datasource", "uid": "victorialogs"}
    expr = f'{{kubernetes.pod_namespace=~"{"|".join(namespaces)}"}}'
    pane = {
        "datasource": "victorialogs",
        "queries": [{"refId": "A", "datasource": datasource, "expr": expr}],
        "range": {"from": "now-1h", "to": "now"},
    }
    panes = quote(json.dumps({"a": pane}, separators=(",", ":")))
    return f"{GRAFANA}/explore?schemaVersion=1&orgId=1&panes={panes}"


def _selector(monitor: dict[str, Any], namespace: str) -> str:
    selector = monitor["selector"]
    if selector is None:
        return "—"
    parts = [f"{key}={value}" for key, value in selector.get("matchLabels", {}).items()]
    parts += [
        f"{e['key']} {e['operator']} {','.join(e.get('values', []))}".strip()
        for e in selector.get("matchExpressions", [])
    ]
    text = ", ".join(parts) or "everything"
    if monitor["namespaces"] is None:
        return f"{text} in every namespace"
    if monitor["namespaces"] != [namespace]:
        return f"{text} in {', '.join(monitor['namespaces'])}"
    return text


def _row(ns: dict[str, Any], monitor: dict[str, Any], now: datetime) -> dict[str, Any]:
    targets = monitor["targets"]
    up = [t for t in targets or [] if t["health"] == "up"]
    if targets is None:
        status, tone = "unknown", "unknown"
    elif not targets:
        status, tone = "no targets", "warn"
    else:
        status = f"{len(up)}/{len(targets)} up"
        tone = "ok" if len(up) == len(targets) else "down"
    return {
        "id": f"{monitor['kind']}/{ns['name']}/{monitor['name']}",
        "name": monitor["name"],
        "kind": monitor["kind"],
        "status": status,
        "tone": tone,
        "problem": tone != "ok",
        "last": _ago(max(t["last_scrape"] for t in targets), now) if targets else "—",
        "targets": [
            {
                "endpoint": t["endpoint"],
                "health": t["health"],
                "last": _ago(t["last_scrape"], now),
                "duration": f"{t['duration_seconds'] * 1000:.0f} ms",
                "error": t["last_error"],
            }
            for t in targets or []
        ],
        "selector": _selector(monitor, ns["name"]),
        "interval": ", ".join(monitor["intervals"]) or "Prometheus's default",
        "job": ", ".join(sorted({t["job"] for t in targets or []})) or "—",
        # A link that can't be worked out is left out (None), not guessed.
        "links": {
            "Dashboard": f"{GRAFANA}/dashboards?query={quote(ns['app'] or monitor['name'])}",
            "Logs": _logs(monitor["namespaces"]) if monitor["namespaces"] else None,
            "Alerts": f"{PROMETHEUS}/alerts?search={quote(ns['name'])}",
            "Argo CD": f"{ARGOCD}/applications/argocd/{monitor['application']}"
            if monitor["application"]
            else None,
        },
    }


def view(
    model: dict[str, Any], path: str, q: str, show: str, app: str | None, now: datetime
) -> dict[str, Any]:
    q = q.strip().lower()
    show = show if show in SHOW else "all"
    every: list[dict[str, Any]] = []
    groups = []
    # Apps first, then the platform; the model has each in name order.
    for ns in sorted(model["namespaces"] or [], key=lambda ns: ns["owner"] != "app"):
        rows = [_row(ns, monitor, now) for monitor in ns["monitors"]]
        every += rows
        shown = [
            row
            for row in rows
            if (app is None or ns["app"] == app)
            and (show != "problems" or row["problem"])
            and (show not in ("apps", "platform") or ns["owner"] == show.removesuffix("s"))
            and any(q in text for text in (ns["name"], row["name"], ns["app"] or ""))
        ]
        if shown:
            groups.append(
                {
                    "name": ns["name"],
                    "owner": ns["owner"],
                    "monitors": sorted(shown, key=lambda row: not row["problem"]),
                }
            )
    targets = [t for row in every for t in row["targets"]]
    return {
        "known": model["namespaces"] is not None,
        "stale": [
            {"name": SOURCES[name][0], "what": SOURCES[name][1], **source}
            for name, source in model["sources"].items()
            if source["stale"]
        ],
        "as_of": min((s["as_of"] for s in model["sources"].values() if s["as_of"]), default=None),
        "summary": {
            "monitors": len(every),
            "kinds": " · ".join(
                f"{sum(row['kind'] == kind for row in every)} {kind}s"
                for kind in ("ServiceMonitor", "PodMonitor", "Probe")
            ),
            "targets_known": model["unclaimed"] is not None,
            "up": sum(t["health"] == "up" for t in targets),
            "targets": len(targets),
            "down": [row["id"].split("/", 1)[1] for row in every if row["tone"] == "down"],
        },
        "groups": groups,
        "shown": sum(len(group["monitors"]) for group in groups),
        "app": app,
        "q": q,
        "show": show,
        "chips": [
            {
                "label": label,
                "on": show == key,
                "href": path
                + "?" * bool(q or key != "all")
                + urlencode({k: v for k, v in (("q", q), ("show", key)) if v and v != "all"}),
            }
            for key, label in SHOW.items()
        ],
        "prometheus": PROMETHEUS,
    }
