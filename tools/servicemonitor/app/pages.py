"""The Overview and Monitors tabs: the read models (app/monitors.py, and app/status.py for
the alerts and the SLO budgets) shaped for templates/overview.html and
templates/monitors.html, and for the shell around both (templates/base.html). Filtering is
done here, from the URL, so a filtered link shows the same rows to whoever opens it.

A monitor has a problem when it has a target that isn't up, no target at all, or targets
nobody knows (Prometheus never answered): unknown is never shown as healthy. So does one
with an alert firing on a job it scrapes, or an SLO with no error budget left. An alert
that is only in a namespace it scrapes is shown, dimmed, and is not the monitor's problem.
The Overview's tiles are marked by the same rules, over each namespace's rows.
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
    "alerts": ("Prometheus's alerts API", "the alerts"),
    "slo": ("Prometheus's query API", "the error budgets"),
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


def _tile(ns: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """A namespace on the Overview: its tile, marked by its worst monitor, and a square for
    each of its targets. What the page doesn't know is unknown, never healthy."""
    targets_known = all(row["tone"] != "unknown" for row in rows)
    alerts_known = all(row["alerts"] is not None for row in rows)
    budgets_known = all(row["slo"] is not None for row in rows)
    if any(row["tone"] in ("down", "warn") for row in rows):  # a target down, or none
        word, tone = "Down", "down"
    elif any(row["alerting"] for row in rows):
        word, tone = "Alert", "warn"
    elif not (targets_known and alerts_known and budgets_known):
        word, tone = "Unknown", "unknown"
    else:
        word, tone = "Healthy", "ok"

    targets = [t for row in rows for t in row["targets"]]
    facts = ["targets unknown"]
    if targets_known:
        facts = [f"{sum(t['health'] == 'up' for t in targets)}/{len(targets)} up"]
        if empty := sum(row["tone"] == "warn" for row in rows):
            facts.append(f"{empty} {'monitor' if empty == 1 else 'monitors'} without targets")
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


def overview(model: dict[str, Any], status: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The Overview tab: every namespace as a tile and as a row of squares."""
    namespaces = _namespaces(model, status, now)
    return {
        **_shell(model, status, [row for _, rows in namespaces for row in rows]),
        "tab": "overview",
        "namespaces": [_tile(ns, rows) for ns, rows in namespaces],
    }
