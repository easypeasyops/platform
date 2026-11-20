"""The laptop node's memory budget (PT-107, PT-121; docs/RUNBOOK.md, Node memory).

The laptop runs the platform, kind's own pods and the apps on one node: 11902Mi allocatable
from Docker Desktop's 12 GB VM (10 GB until PT-121, when the platform no longer fitted).
The node needs 3 GiB free: below it the VM swaps and etcd stalls (PT-104). Measured in
Prometheus (working set, each container once) on 2026-11-19, kind's own pods peaked at 2129Mi
since the API server's GOMEMLIMIT (metrics-server left out: it is the platform's, below) and
the apps at 1280Mi over 7 days. What is left is the platform's (REQUESTS):

  11902 - 3072 free - 2129 kind's own - 1280 the apps = 5421Mi

and its requests, summed, must fit in it. Each is the component's median working set over 2
days plus 10%, rounded up to 8Mi (RUNBOOK, Node memory): their sum is close to the platform's
real peak (4875Mi), while summing each container's own peak would count spikes that never
coincide. LIMITS is 120% of the node's allocatable: the platform's limits, summed.

The test renders what test_cpu_budget.py renders (Argo CD and every child Application,
monitoring on) and sums the memory of the same long-running pods, counted the same way.

Over budget? Don't raise it. Measure the new component the same way and size its request,
or lower another to make room, and say which in the PR. A request below real use only hides
memory from the scheduler; it doesn't free any. A new kind's own or apps' peak, or a bigger
VM, changes the constants below, each from Prometheus.
"""

from pathlib import Path
from typing import Any

import pytest

from tests.test_cpu_budget import _application, _argocd
from tests.test_platform import _render

ALLOCATABLE = 11902
FREE = 3072
KIND_OWN = 2129
APPS = 1280
REQUESTS = ALLOCATABLE - FREE - KIND_OWN - APPS
LIMITS = int(1.2 * ALLOCATABLE)
# Envoy Gateway adds a shutdown-manager container to each proxy pod with this request and
# no limit (its default; the live proxy pod requests 96Mi for our 64Mi).
SHUTDOWN_MANAGER = 32
MEBIBYTES = {
    "Ki": 2**-10,
    "Mi": 1,
    "Gi": 2**10,
    "K": 1e3 / 2**20,
    "M": 1e6 / 2**20,
    "G": 1e9 / 2**20,
}


def _mebibytes(quantity: str | int) -> float:
    text = str(quantity)
    for unit, factor in MEBIBYTES.items():
        if text.endswith(unit):
            return float(text.removesuffix(unit)) * factor
    return float(text) / 2**20


def _memory(resources: dict[str, Any] | None, kind: str) -> float:
    return _mebibytes(((resources or {}).get(kind) or {}).get("memory", 0))


def _pod(spec: dict[str, Any], kind: str) -> float:
    sidecars = [c for c in spec.get("initContainers") or [] if c.get("restartPolicy") == "Always"]
    return sum(_memory(c.get("resources"), kind) for c in spec["containers"] + sidecars)


def _owners(docs: list[dict[str, Any]], kind: str) -> dict[str, float]:
    """Memory requests or limits of each long-running pod owner among the documents."""
    out = {}
    for d in docs:
        spec = d.get("spec") or {}
        name = f"{d['kind']}/{d['metadata']['name']}"
        if d["kind"] in {"Deployment", "StatefulSet"}:
            out[name] = (spec.get("replicas") or 1) * _pod(spec["template"]["spec"], kind)
        elif d["kind"] == "DaemonSet":
            out[name] = _pod(spec["template"]["spec"], kind)
        elif d["kind"] in {"Prometheus", "Alertmanager"}:
            out[name] = (spec.get("replicas") or 1) * _memory(spec.get("resources"), kind)
        elif d["kind"] == "EnvoyProxy":
            deployment = spec["provider"]["kubernetes"]["envoyDeployment"]
            per_pod = _memory(deployment["container"].get("resources"), kind)
            per_pod += SHUTDOWN_MANAGER if kind == "requests" else 0
            out[name] = (deployment.get("replicas") or 1) * per_pod
    return out


@pytest.mark.integration
def test_the_platforms_memory_stays_inside_the_laptop_nodes_budget(tmp_path: Path) -> None:
    docs = {"argocd": _argocd()}
    for app in _render("components.slack=false"):  # skips without helm
        docs[app["metadata"]["name"]] = _application(app, tmp_path)
    for kind, budget in (("requests", REQUESTS), ("limits", LIMITS)):
        owners = {f"{a}: {o}": m for a, d in docs.items() for o, m in _owners(d, kind).items()}
        total = round(sum(owners.values()))
        table = "\n".join(f"  {m:5.0f}Mi  {o}" for o, m in sorted(owners.items()))
        assert total <= budget, (
            f"the platform's memory {kind} total {total}Mi, over its {budget}Mi budget "
            f"(this file's docstring says how to stay inside it)\n{table}"
        )
