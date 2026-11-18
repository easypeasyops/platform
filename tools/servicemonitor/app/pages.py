"""The Overview and Monitors tabs: the read models (app/monitors.py, and app/status.py for
the alerts and the SLO budgets) shaped for templates/overview.html and
templates/monitors.html, and for the shell around both (templates/base.html). Filtering is
done here, from the URL, so a filtered link shows the same rows to whoever opens it.

A monitor has a problem when it has a target that isn't up, no target at all, or targets
nobody knows (Prometheus never answered): unknown is never shown as healthy. So does one
with an alert firing on a job it scrapes, or an SLO with no error budget left. An alert
that is only in a namespace it scrapes is shown, dimmed, and is not the monitor's problem.
The Overview's tiles are marked by the same rules, over each namespace's rows and its pods
(a workload with none ready is Down, one short for over a minute Alert) and its containers
(a restart or an OOM kill in the hour is Alert: app/containers.py) and its CronJobs (one
late or whose latest Job failed is Alert, one that never ran Unknown), and so are its
platform components' lines, over their monitors' rows and their own alerts.
"""

import json
import os
from datetime import datetime
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import quote, urlencode

from app import containers

# Where a browser reaches the tools: the front door's `tools` listener on the laptop.
GRAFANA = os.environ.get("GRAFANA_LINK_URL", "http://grafana.localhost:8090")
PROMETHEUS = os.environ.get("PROMETHEUS_LINK_URL", "http://prometheus.localhost:8090")
ARGOCD = os.environ.get("ARGOCD_LINK_URL", "http://argocd.localhost:8090")

SHOW = {"all": "All", "problems": "Problems only", "apps": "Apps", "platform": "Platform"}
# Each source of the read model: what the page calls it, and what is unknown without it.
SOURCES = {
    "kubernetes": ("The Kubernetes API", "the monitors"),
    "prometheus": ("Prometheus", "the targets"),
    "alerts": ("Prometheus's alerts API", "the alerts"),
    "slo": ("Prometheus's query API", "the error budgets"),
    "replicas": ("Prometheus's query API", "the pods ready"),
    "containers": ("Prometheus's query API", "the restarts and memory"),
    "cronjobs": ("Prometheus's query API", "the CronJobs"),
}
NO_PODS = {"ready": 0, "desired": 0, "short": 0, "none": 0}  # no Deployment or StatefulSet


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


def _percent(remaining: float) -> str:
    return f"{round(remaining * 100)}%"  # the real number, below zero too: -1501%


def _budget(slo: dict[str, Any] | None) -> dict[str, Any] | None:
    """The SLO cell: the lowest budget (`left`), or why there is none (`reason`). None:
    unknown, the query never answered."""
    if slo is None:
        return None
    lowest = slo["lowest"]
    if lowest is None:
        # Budgets without a number: no request in the SLO's window to compute one from.
        return {"left": None, "reason": slo["reason"] or "no requests in the SLO's window"}
    return {
        "left": _percent(lowest["remaining"]),
        "name": lowest["slo"],
        "remaining": lowest["remaining"],
        "width": round(min(max(lowest["remaining"], 0), 1) * 100),  # the bar never overflows
        "spent": lowest["remaining"] <= 0,
    }


def _row(
    ns: dict[str, Any], monitor: dict[str, Any], state: dict[str, Any], now: datetime
) -> dict[str, Any]:
    targets = monitor["targets"]
    up = [t for t in targets or [] if t["health"] == "up"]
    if targets is None:
        status, tone = "unknown", "unknown"
    elif not targets:
        status, tone = "no targets", "warn"
    else:
        status = f"{len(up)}/{len(targets)} up"
        tone = "ok" if len(up) == len(targets) else "down"
    # None: unknown, Prometheus never said which alerts fire.
    alerts = state.get("alerts") and [
        {
            "name": alert["name"],
            "summary": alert["summary"],
            "href": PROMETHEUS + alert["prometheus_path"],
            "dim": alert["match"] == "namespace",
            "labels": alert["labels"],
        }
        for alert in state["alerts"]
    ]
    slo = _budget(state.get("slo"))
    alerting = any(not alert["dim"] for alert in alerts or []) or bool(
        slo and slo["left"] and slo["spent"]
    )
    # Where its targets are, which is where its alerts are: not always where it lives.
    # Prometheus's alerts page searches rule names, not an alert's labels: the ALERTS
    # series has the namespace.
    scrapes = "|".join(state.get("scrapes", [ns["name"]]))
    alerts_query = quote(f'ALERTS{{namespace=~"{scrapes}"}}')
    return {
        "id": f"{monitor['kind']}/{ns['name']}/{monitor['name']}",
        "name": monitor["name"],
        "kind": monitor["kind"],
        "status": status,
        "tone": tone,
        "dot": "warn" if alerting and tone == "ok" else tone,
        "problem": tone != "ok" or alerting,
        "alerting": alerting,
        "alerts": alerts,
        "slo": slo,
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
            "Alerts": f"{PROMETHEUS}/query?g0.tab=table&g0.expr={alerts_query}",
            "Argo CD": f"{ARGOCD}/applications/argocd/{monitor['application']}"
            if monitor["application"]
            else None,
        },
    }


def _namespaces(
    model: dict[str, Any], status: dict[str, Any], now: datetime
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Every namespace with its monitors' rows. `status` is status.read()'s answer over
    `model`, monitors.read()'s."""
    # A monitor the status doesn't have (it appeared between the two reads): unknown.
    statuses = {
        (ns["name"], monitor["kind"], monitor["name"]): monitor
        for ns in status["namespaces"] or []
        for monitor in ns["monitors"]
    }
    # Apps first, then the platform; the model has each in name order.
    return [
        (
            ns,
            [
                _row(ns, m, statuses.get((ns["name"], m["kind"], m["name"]), {}), now)
                for m in ns["monitors"]
            ],
        )
        for ns in sorted(model["namespaces"] or [], key=lambda ns: ns["owner"] != "app")
    ]


def _shell(
    model: dict[str, Any], status: dict[str, Any], every: list[dict[str, Any]]
) -> dict[str, Any]:
    """What templates/base.html shows on every tab: the sources that don't answer and the
    summary line, over every monitor's row."""
    lowest = status["summary"]["lowest_budget"]
    targets = [t for row in every for t in row["targets"]]
    return {
        "known": model["namespaces"] is not None,
        "stale": [
            {"name": SOURCES[name][0], "what": SOURCES[name][1], **source}
            for name, source in status["sources"].items()
            if source["stale"]
        ],
        "as_of": min((s["as_of"] for s in status["sources"].values() if s["as_of"]), default=None),
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
            "alerts": status["summary"]["alerts_firing"],  # None: unknown
            # Firing, and on no monitor's row: counted, so named here.
            "unattached": [
                {"name": alert["name"], "href": PROMETHEUS + alert["prometheus_path"]}
                for alert in status["unattached"] or []
            ],
            "budgets_known": status["sources"]["slo"]["as_of"] is not None,
            "lowest": lowest
            and {
                "left": _percent(lowest["remaining"]),
                "where": f"{lowest['namespace']} · {lowest['slo']}",
                "spent": lowest["remaining"] <= 0,
            },
        },
        "prometheus": PROMETHEUS,
    }


def view(
    model: dict[str, Any],
    status: dict[str, Any],
    path: str,
    q: str,
    show: str,
    app: str | None,
    now: datetime,
) -> dict[str, Any]:
    """The Monitors tab."""
    q = q.strip().lower()
    show = show if show in SHOW else "all"
    namespaces = _namespaces(model, status, now)
    groups = []
    for ns, rows in namespaces:
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
                    "pods": _pods(status["replicas"], ns["name"]),
                    "containers": containers.facts(status["containers"], ns["name"]),
                    "cronjobs": _cron(status["cronjobs"], ns["name"])["rows"],
                    "monitors": sorted(shown, key=lambda row: not row["problem"]),
                }
            )
    return {
        **_shell(model, status, [row for _, rows in namespaces for row in rows]),
        "tab": "monitors",
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
    }


def _mark(
    down: bool, alerting: bool, known: bool, pods: dict[str, int] | None = NO_PODS
) -> tuple[str, str]:
    """A tile's, a component's or a namespace's pods' word and tone: Down wins over Alert,
    Alert over Unknown, and Healthy only when everything is known. `pods` (app/status.py's
    replicas; None: unknown) are Down with a workload none ready, Alert with one short for
    over a minute."""
    if down or (pods is not None and pods["none"]):
        return "Down", "down"
    if alerting or (pods is not None and pods["short"]):
        return "Alert", "warn"
    if not known or pods is None:
        return "Unknown", "unknown"
    return "Healthy", "ok"


def _pods(replicas: dict[str, dict[str, int]] | None, namespace: str) -> dict[str, Any]:
    """A namespace's pods: their counts (None: unknown), what its tile and its group say
    (None: it has no Deployment or StatefulSet) and their tone, by _mark."""
    counts = None if replicas is None else replicas.get(namespace, NO_PODS)
    text = "pods unknown" if counts is None else f"pods {counts['ready']}/{counts['desired']} ready"
    word, tone = _mark(False, False, True, counts)
    return {
        "counts": counts,
        "text": None if counts == NO_PODS else text,
        "word": word,
        "tone": tone,
    }


def _cron(crons: dict[str, list[dict[str, Any]]] | None, namespace: str) -> dict[str, Any]:
    """A namespace's CronJobs (app/cronjobs.py; None: unknown): their rows, what its tile
    says (None: it has none), whether one is late or failed, and whether all are known. A
    suspended one is left out of the count."""
    rows = None if crons is None else crons.get(namespace, [])
    counted = [row for row in rows or [] if row["state"] != "suspended"]
    on_time = sum(row["state"] == "on time" for row in counted)
    parts = [f"{on_time}/{len(counted)} on time" if counted else ""]
    if suspended := len(rows or []) - len(counted):
        parts.append(f"{suspended} suspended")
    return {
        "rows": rows,
        "text": "cron unknown"
        if rows is None
        else rows and "cron " + " · ".join(filter(None, parts)),
        "problem": any(row["tone"] in ("down", "warn") for row in counted),
        "known": rows is not None and all(row["tone"] != "unknown" for row in counted),
    }


def _tile(ns: dict[str, Any], rows: list[dict[str, Any]], status: dict[str, Any]) -> dict[str, Any]:
    """A namespace on the Overview: its tile, marked by its worst monitor and its pods, and
    a square for each of its targets. What the page doesn't know is unknown, never healthy."""
    targets_known = all(row["tone"] != "unknown" for row in rows)
    alerts_known = all(row["alerts"] is not None for row in rows)
    budgets_known = all(row["slo"] is not None for row in rows)
    pods = _pods(status["replicas"], ns["name"])
    mine = containers.facts(status["containers"], ns["name"])
    cron = _cron(status["cronjobs"], ns["name"])
    word, tone = _mark(
        any(row["tone"] in ("down", "warn") for row in rows),  # a target down, or none
        any(row["alerting"] for row in rows) or mine["alerting"] or cron["problem"],
        targets_known
        and alerts_known
        and budgets_known
        and mine["tone"] != "unknown"
        and cron["known"],
        pods["counts"],
    )

    targets = [t for row in rows for t in row["targets"]]
    facts = ["targets unknown"]
    if targets_known:
        facts = [f"{sum(t['health'] == 'up' for t in targets)}/{len(targets)} up"]
        if empty := sum(row["tone"] == "warn" for row in rows):
            facts.append(f"{empty} {'monitor' if empty == 1 else 'monitors'} without targets")
    if pods["text"]:
        facts.append(pods["text"])
    if mine["text"]:
        facts.append(mine["text"])
    if cron["text"]:
        facts.append(cron["text"])
    # An alert is on the row of every monitor that scrapes its namespace: counted once.
    alerts = {
        tuple(sorted(alert["labels"].items())) for row in rows for alert in row["alerts"] or []
    }
    facts.append(
        f"{len(alerts)} {'alert' if len(alerts) == 1 else 'alerts'}"
        if alerts_known
        else "alerts unknown"
    )
    budgets = [row["slo"] for row in rows if row["slo"] and row["slo"]["left"]]
    if not budgets_known:
        facts.append("SLO unknown")
    elif budgets:
        facts.append(f"SLO {min(budgets, key=lambda slo: slo['remaining'])['left']}")
    monitors = f"/monitors?q={quote(ns['name'])}"
    return {
        "name": ns["name"],
        "owner": ns["owner"],
        "word": word,
        "tone": tone,
        "facts": " · ".join(facts),
        "href": monitors,
        # None: unknown, Prometheus never said which targets there are.
        "squares": [
            {
                "label": f"{row['name']} · {t['endpoint']} · {t['health']}",
                # Prometheus's own "unknown": a target it hasn't scraped yet.
                "health": t["health"] if t["health"] in ("up", "down") else "unknown",
                "href": f"{monitors}#{row['id']}",  # its monitor's row
            }
            for row in rows
            for t in row["targets"]
        ]
        if targets_known
        else None,
    }


def _component(
    component: dict[str, Any], rows: list[dict[str, Any]], firing: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """A platform component's line (app/components.py): its monitors' rows by their
    Kind/namespace/name, the firing alerts with one of its names, and its signal."""
    mine = [row for row in rows if any(fnmatchcase(row["id"], m) for m in component["monitors"])]
    missing = [
        m for m in component["monitors"] if not any(fnmatchcase(row["id"], m) for row in rows)
    ]
    alerts = firing and [
        {"name": alert["name"], "href": PROMETHEUS + alert["prometheus_path"]}
        for alert in firing
        if any(fnmatchcase(alert["name"], name) for name in component["alerts"])
    ]
    targets_known = all(row["tone"] != "unknown" for row in mine)
    reading = component["reading"]
    word, tone = _mark(
        bool(missing) or any(row["tone"] in ("down", "warn") for row in mine),
        bool(alerts),
        targets_known and alerts is not None and reading["error"] is None,
    )
    targets = [t for row in mine for t in row["targets"]]
    return {
        "name": component["name"],
        "word": word,
        "tone": tone,
        "targets": f"{sum(t['health'] == 'up' for t in targets)}/{len(targets)} up"
        if targets_known
        else "targets unknown",
        "missing": missing,
        "alerts": alerts,
        "signal": reading["text"],
        "error": reading["error"],
        "dashboard": f"{GRAFANA}/d/{component['dashboard']}" if "dashboard" in component else None,
    }


def overview(
    model: dict[str, Any],
    status: dict[str, Any],
    components: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """The Overview tab: every namespace as a tile and as a row of squares, and each
    platform component as a line."""
    namespaces = _namespaces(model, status, now)
    every = [row for _, rows in namespaces for row in rows]
    # Every alert firing, once: the monitors' rows share the ones of their namespace.
    firing = None
    if status["unattached"] is not None:
        alerts = [a for ns in status["namespaces"] for m in ns["monitors"] for a in m["alerts"]]
        firing = list(
            {tuple(sorted(a["labels"].items())): a for a in alerts + status["unattached"]}.values()
        )
    return {
        **_shell(model, status, every),
        "tab": "overview",
        "namespaces": [_tile(ns, rows, status) for ns, rows in namespaces],
        "components": [_component(c, every, firing) for c in components],
    }
