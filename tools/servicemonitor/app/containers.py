"""Each namespace's containers, from Prometheus (kube-state-metrics, the kubelet's cAdvisor
and node-exporter): the restarts in the last hour, the containers OOM-killed in it, and the
container closest to its memory limit. A pod that restarts but is up at every scrape, or is
killed for memory and comes back, never shows in "pods R/D ready".

The laptop's node reboots after a night asleep, and every pod restarts as it comes back,
some more than once while what they need starts. That is not a problem, so restarts are
counted from GRACE after the node's boot (node-exporter's `node_boot_time_seconds`), over at
most the last hour: in the first GRACE after a boot none are, and the window then grows back
to an hour. A shorter sleep pauses the node instead: its clock jumps, nothing reboots, and
what restarts then is counted. An OOM kill is never the sleep's doing: it counts for an hour
from the kill (`kube_pod_container_status_last_terminated_timestamp`), boot or not.
"""

from collections.abc import Awaitable, Callable
from typing import Any

HOUR = 3600
GRACE = 600  # seconds after the node's boot when restarts aren't counted
UPTIME = "time() - max(node_boot_time_seconds)"
RESTARTS = "round(sum by (namespace) (increase(kube_pod_container_status_restarts_total[{}s])))"
_CONTAINER = "namespace, pod, container"
# When each container whose last end was an OOM kill was killed: in the hour, it counts.
OOM = (
    f"kube_pod_container_status_last_terminated_timestamp and on ({_CONTAINER})"
    ' (kube_pod_container_status_last_terminated_reason{reason="OOMKilled"} == 1)'
)
MEMORY = (
    f"topk by (namespace) (1, max by ({_CONTAINER}) "
    '(container_memory_working_set_bytes{container!=""})'
    f" / on ({_CONTAINER}) max by ({_CONTAINER}) "
    '(kube_pod_container_resource_limits{resource="memory"}))'
)
HIGH = 0.9  # of its limit: shown as near it, not alerted on (the stack's own alerts are)


def _name(series: dict[str, Any]) -> str:
    return f"{series['metric']['pod']}/{series['metric']['container']}"


async def read(query: Callable[[str], Awaitable[list[dict[str, Any]]]]) -> dict[str, Any]:
    """`window` (seconds the restarts are counted over) and, per namespace with containers,
    `restarts`, `oom` (the containers OOM-killed in the hour) and `memory` (its container
    closest to its memory limit, None: none has a limit). `query` answers a PromQL query's
    series."""
    uptime = await query(UPTIME)
    # No node-exporter: no boot to leave out, the whole hour counts.
    window = min(HOUR, int(float(uptime[0]["value"][1])) - GRACE) if uptime else HOUR
    found: dict[str, dict[str, Any]] = {}

    def namespace(series: dict[str, Any]) -> dict[str, Any]:
        return found.setdefault(
            series["metric"]["namespace"], {"restarts": 0, "oom": [], "memory": None}
        )

    if window >= 60:
        for series in await query(RESTARTS.format(window)):
            namespace(series)["restarts"] = int(float(series["value"][1]))
    for series in await query(OOM):
        at, killed = (float(v) for v in series["value"])  # Prometheus's now, and the kill's
        if at - killed < HOUR:
            namespace(series)["oom"].append(_name(series))
    for series in await query(MEMORY):
        namespace(series)["memory"] = {
            "used": float(series["value"][1]),
            "container": _name(series),
        }
    return {"window": max(window, 0), "namespaces": found}


def facts(containers: dict[str, Any] | None, name: str) -> dict[str, Any]:
    """What a namespace's tile and group say about its containers (`text`, None: nothing to
    say, no container), whether that makes it Alert, and its tone. `containers` is read()'s
    answer, None: unknown."""
    if containers is None:
        return {"text": "restarts unknown", "alerting": False, "tone": "unknown"}
    mine = containers["namespaces"].get(name)
    if mine is None:
        return {"text": None, "alerting": False, "tone": "ok"}
    window = containers["window"]
    if window < 60:
        said = ["restarts not counted yet: the node has just booted"]
    else:
        said = [
            f"restarts {mine['restarts']} in {'1 h' if window == HOUR else f'{window // 60} min'}"
        ]
    if mine["oom"]:
        said.append("OOM killed")
    if mine["memory"]:
        used = mine["memory"]["used"]
        said.append(f"memory {round(used * 100)}% of limit{' (near it)' if used >= HIGH else ''}")
    alerting = bool(mine["restarts"] or mine["oom"])
    return {"text": " · ".join(said), "alerting": alerting, "tone": "warn" if alerting else "ok"}
