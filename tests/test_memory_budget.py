"""The laptop node's memory budget (PT-107; docs/RUNBOOK.md, Node memory).

The laptop runs the platform, kind's own pods and the apps on one node with 9898Mi
allocatable (Docker Desktop's 10 GB VM). Measured on 2026-11-19 (Prometheus, working set,
7 days):

                     requests   limits   working set   7-day peak
    the platform      4008Mi   11200Mi      4108Mi        4837Mi
    kind's own         290Mi     340Mi      1973Mi        2737Mi
    the apps           256Mi     512Mi       116Mi        1279Mi

The node needs about 3 GiB free (below it the VM swaps and etcd stalls, PT-104). With
that, kind's own pods and the apps' peak, the platform would get 9898 - 3072 - 2048 - 1280
= 3498Mi: it already uses more. So these budgets are a cap on growth, not a fit:

  REQUESTS  4096Mi  today's 4008Mi rounded up: the platform's requests, summed
  LIMITS   11877Mi  120% of the node's allocatable: the platform's limits, summed

Freeing memory and tightening REQUESTS to keep 3 GiB free is PT-121.

The test renders what test_cpu_budget.py renders (Argo CD and every child Application,
monitoring on) and sums the memory of the same long-running pods, counted the same way.

Over budget? Don't raise it. Set a request to the component's measured peak with a margin
(RUNBOOK, Node memory), or lower another to make room, and say which in the PR. A request
below real use only hides memory from the scheduler; it doesn't free any.
"""

from pathlib import Path
from typing import Any

import pytest

from tests.test_cpu_budget import _application, _argocd
from tests.test_platform import _render

ALLOCATABLE = 9898
REQUESTS = 4096
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
