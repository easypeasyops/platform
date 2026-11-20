"""The CI node's CPU budget (PT-71; docs/RUNBOOK.md, The CI node's CPU budget).

links' platform e2e runs the whole platform and shortly on one kind node with 4000m CPU
allocatable. The scheduler places a pod only where its CPU *request* fits, so the requests
must leave room for prod's canary: 2 old and 2 new pods (400m each in the e2e, links #171;
500m in prod). When they didn't, the
canary's last pod stayed Pending (`Insufficient cpu`) and the e2e failed on links' lock PR,
not on the platform PR that added the request (links #169).

The platform's share, from what the e2e measured (links run 44029453438's node diagnostics):

    4000m  the node's allocatable CPU
  -  950m  kind's own pods: kube-apiserver 250, kube-controller-manager 200, coredns 2 x 100,
           etcd, kindnet and kube-scheduler 100 each (kubeadm's and kind's; not set here)
  - 1645m  one app at its canary's peak: shortly's 4 prod pods at 400m (the e2e's request,
           E2E_PROD_CPU in links' scripts/k8s-e2e.sh), its staging pod 25m, a database in
           each namespace 10m
  -  500m  left free at that step: room for one more app pod (PT-71)
  =  905m  what everything the platform installs may request, summed (BUDGET)

The test renders what the e2e installs (POLICIES=1, BACKUPS=1, monitoring on): Argo CD, and
every child Application of the platform chart, Helm charts with our values and the git
directories as Argo CD reads them. It sums the CPU requests of the pods that keep running:
Deployments and StatefulSets times their replicas, DaemonSets once (one node), and the pods
operators make from our resources (Prometheus, Alertmanager, the Envoy proxy). Jobs and
CronJobs are left out: they finish before the canary step.

Over budget? Don't raise it: a request here is CPU the canary can't have. Set the new
request to the component's measured peak with a margin (RUNBOOK: Prometheus, a 5-minute rate
over a day), or lower another request to make room, and say which in the PR.
"""

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.test_platform import ROOT, _render, _sources

ALLOCATABLE = 4000
KIND_OWN = 950
APP_AT_CANARY = 4 * 400 + 25 + 2 * 10
HEADROOM = 500
BUDGET = ALLOCATABLE - KIND_OWN - APP_AT_CANARY - HEADROOM
# Envoy Gateway adds a shutdown-manager container to each proxy pod with this request (its
# default; the e2e's diagnostics show the proxy pod at 50m,10m).
SHUTDOWN_MANAGER = 10


def _millicores(quantity: str | int | float) -> int:
    text = str(quantity)
    return int(text[:-1]) if text.endswith("m") else round(float(text) * 1000)


def _cpu(resources: dict[str, Any] | None) -> int:
    return _millicores(((resources or {}).get("requests") or {}).get("cpu", 0))


def _pod(spec: dict[str, Any]) -> int:
    sidecars = [c for c in spec.get("initContainers") or [] if c.get("restartPolicy") == "Always"]
    return sum(_cpu(c.get("resources")) for c in spec["containers"] + sidecars)


def _requests(docs: list[dict[str, Any]]) -> dict[str, int]:
    """CPU requested by each long-running pod owner among the rendered documents."""
    out = {}
    for d in docs:
        kind, spec = d["kind"], d.get("spec") or {}
        name = f"{kind}/{d['metadata']['name']}"
        if kind in {"Deployment", "StatefulSet"}:
            out[name] = (spec.get("replicas") or 1) * _pod(spec["template"]["spec"])
        elif kind == "DaemonSet":
            out[name] = _pod(spec["template"]["spec"])
        elif kind in {"Prometheus", "Alertmanager"}:
            out[name] = (spec.get("replicas") or 1) * _cpu(spec.get("resources"))
        elif kind == "EnvoyProxy":
            deployment = spec["provider"]["kubernetes"]["envoyDeployment"]
            per_pod = _cpu(deployment["container"].get("resources")) + SHUTDOWN_MANAGER
            out[name] = (deployment.get("replicas") or 1) * per_pod
    return out


def _run(*args: str) -> list[dict[str, Any]]:
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _argocd() -> list[dict[str, Any]]:
    """Argo CD, as scripts/cluster.sh installs it (Helm, before the platform)."""
    pin = yaml.safe_load((ROOT / "deploy/argocd/platform/values.yaml").read_text())["charts"]
    return _run(
        *("helm", "template", "argocd", f"{pin['argocd']['repo']}@{pin['argocd']['digest']}"),
        *("--namespace", "argocd", "--values", str(ROOT / "deploy/kind/argocd-values.yaml")),
    )


def _application(app: dict[str, Any], tmp: Path) -> list[dict[str, Any]]:
    """What one child Application deploys, read from its sources as Argo CD reads them."""
    namespace = app["spec"]["destination"]["namespace"]
    docs = []
    for source in _sources(app):
        helm = source.get("helm")
        if source["repoURL"].startswith("oci://") and helm is not None:
            values = [str(ROOT / f.removeprefix("$values/")) for f in helm["valueFiles"]]
            if "valuesObject" in helm:
                extra = tmp / f"{app['metadata']['name']}.yaml"
                extra.write_text(yaml.safe_dump(helm["valuesObject"]))
                values.append(str(extra))
            chart = f"{source['repoURL']}@{source['targetRevision']}"
            args = [arg for v in values for arg in ("--values", v)]
            docs += _run("helm", "template", helm["releaseName"], chart, "-n", namespace, *args)
        elif "path" in source:
            path = ROOT / source["path"]
            if (path / "kustomization.yaml").exists():
                docs += _run("kubectl", "kustomize", str(path))
                continue
            include = (source.get("directory") or {}).get("include", "*.yaml")
            for name in include.strip("{}").split(","):
                for f in sorted(path.glob(name)):
                    docs += [d for d in yaml.safe_load_all(f.read_text()) if d]
    return docs


@pytest.mark.integration
def test_the_platform_leaves_room_for_an_apps_canary_on_the_ci_node(tmp_path: Path) -> None:
    apps = _render("components.slack=false")  # skips without helm; the e2e has no Slack
    requested = _requests(_argocd())
    for app in apps:
        for owner, cpu in _requests(_application(app, tmp_path)).items():
            requested[f"{app['metadata']['name']}: {owner}"] = cpu
    total = sum(requested.values())
    table = "\n".join(f"  {cpu:4}m  {owner}" for owner, cpu in sorted(requested.items()))
    assert total <= BUDGET, (
        f"the platform requests {total}m CPU on kind, over its {BUDGET}m budget: links' e2e "
        f"would have {ALLOCATABLE - KIND_OWN - APP_AT_CANARY - total}m free at prod's canary, "
        f"not {HEADROOM}m (this file's docstring says how to stay inside it)\n{table}"
    )
