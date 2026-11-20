"""The chaos drill (PT-110; docs/RUNBOOK.md, The chaos drill).

Kills one platform pod and checks that the component comes back and that its alert fires
and resolves, each within a deadline. Prints the times; exits 1 if any step missed its
deadline.

The component is the External Secrets controller: while it is away nothing is refreshed,
but every Secret keeps its value and nothing that runs notices. Its alert,
ExternalSecretsDown, needs the controller unscraped for 10 minutes, and the Deployment
replaces a deleted pod in seconds. So the drill cuts the scrape for as long as the alert
needs: a NetworkPolicy that lets nothing in to the controller's pods (the controller only
makes connections, it serves none but its metrics), then one delete, so the new pod starts
behind it. Deleting each new pod instead churned 3-4 pods a second and most of the node's
CPU (run 1, 2026-11-19). Argo CD doesn't track the policy, and the Deployment is never
changed. The drill removes the policy when it stops, SIGTERM included; a policy left
behind keeps the alert firing (RUNBOOK says what to delete).

Runs in the cluster (the CronJob beside this file), with the standard library only.
"""

import json
import os
import signal
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

NAMESPACE = "external-secrets"
DEPLOYMENT = "external-secrets"
LABEL = ("app.kubernetes.io/name", "external-secrets")  # the controller's pods, only
POLICY = "chaos-drill"  # the NetworkPolicy that cuts the scrape
ALERT = "ExternalSecretsDown"  # for: 10m (deploy/argocd/platform/files/external-secrets-rules.yaml)
ALERTMANAGER = "http://kps-alertmanager.monitoring.svc:9093"
API = "https://kubernetes.default.svc"
SA = "/var/run/secrets/kubernetes.io/serviceaccount"

# Deadlines, in seconds. Firing: the rule's 10 minutes, plus the 30 s scrape (and its 25 s
# timeout), the 15 s evaluation and Alertmanager's receipt, with room. Overridable to show a
# failing drill.
FIRE_WITHIN = int(os.environ.get("DRILL_FIRE_WITHIN", "900"))
READY_WITHIN = 300  # the new pod scheduled, started and Ready
RESOLVE_WITHIN = 300  # scraped again (30 s), evaluated (15 s), resolved in Alertmanager


@dataclass
class Run:
    """Seconds from the delete; None: it didn't happen before its deadline."""

    ready: float | None = None
    fired: float | None = None
    restored: float | None = None  # the scrape let back in
    resolved: float | None = None


def verdict(run: Run) -> list[str]:
    """What went wrong; empty when the drill passed."""
    problems = []
    if run.ready is None:
        problems.append(f"{NAMESPACE}/{DEPLOYMENT} wasn't Ready within {READY_WITHIN}s")
    if run.fired is None:
        problems.append(f"{ALERT} didn't reach Alertmanager within {FIRE_WITHIN}s of the outage")
    elif run.resolved is None:
        problems.append(f"{ALERT} didn't resolve within {RESOLVE_WITHIN}s of the scrape restored")
    return problems


def report(run: Run) -> str:
    def at(t: float | None) -> str:
        return "never" if t is None else f"{t:.0f}s"

    return (
        f"{DEPLOYMENT} Ready at {at(run.ready)}, {ALERT} fired at {at(run.fired)}, "
        f"scrape restored at {at(run.restored)}, alert resolved at {at(run.resolved)} "
        "(seconds from the pod's delete)"
    )


def _kube(method: str, path: str, body: Any = None) -> Any:
    with open(f"{SA}/token") as f:
        token = f.read()
    request = urllib.request.Request(  # noqa: S310 (the API server's fixed https URL)
        API + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    context = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    with urllib.request.urlopen(request, timeout=30, context=context) as response:  # noqa: S310
        return json.load(response)


def alert_active() -> bool:
    query = urllib.parse.urlencode({"filter": f'alertname="{ALERT}"'})
    url = f"{ALERTMANAGER}/api/v2/alerts?{query}"
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 (fixed http URL)
        return bool(json.load(response))


def ready() -> bool:
    d = _kube("GET", f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{DEPLOYMENT}")
    want = d["spec"].get("replicas", 1)
    return bool(want >= 1 and d["status"].get("readyReplicas", 0) >= want)


def policy_exists() -> bool:
    try:
        _kube("GET", f"/apis/networking.k8s.io/v1/namespaces/{NAMESPACE}/networkpolicies/{POLICY}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise
    return True


def cut_scrape() -> None:
    policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": POLICY, "labels": {"app.kubernetes.io/name": "chaos-drill"}},
        "spec": {"podSelector": {"matchLabels": dict([LABEL])}, "policyTypes": ["Ingress"]},
    }
    _kube("POST", f"/apis/networking.k8s.io/v1/namespaces/{NAMESPACE}/networkpolicies", policy)


def restore_scrape() -> None:
    try:
        _kube(
            "DELETE", f"/apis/networking.k8s.io/v1/namespaces/{NAMESPACE}/networkpolicies/{POLICY}"
        )
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise


def kill_pods() -> None:
    selector = urllib.parse.urlencode({"labelSelector": "=".join(LABEL)})
    pods = _kube("GET", f"/api/v1/namespaces/{NAMESPACE}/pods?{selector}")["items"]
    for pod in pods:
        _kube("DELETE", f"/api/v1/namespaces/{NAMESPACE}/pods/{pod['metadata']['name']}")


def wait_for(check: Callable[[], bool], within: int, start: float) -> float | None:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if check():
            return time.monotonic() - start
        time.sleep(5)
    return None


def drill() -> Run:
    run = Run()
    cut_scrape()
    start = time.monotonic()
    try:
        kill_pods()
        run.ready = wait_for(ready, READY_WITHIN, start)
        run.fired = wait_for(alert_active, max(0, FIRE_WITHIN - int(run.ready or 0)), start)
    finally:  # whatever happened, Prometheus scrapes the controller again
        restore_scrape()
    run.restored = time.monotonic() - start
    if run.fired is not None:
        run.resolved = wait_for(lambda: not alert_active(), RESOLVE_WITHIN, start)
    return run


def _stop(signum: int, frame: object) -> None:
    raise SystemExit(143)  # runs the finally above: the policy goes, the scrape is back


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)  # PID 1 ignores SIGTERM unless it handles it
    if not ready():
        print(f"not starting: {NAMESPACE}/{DEPLOYMENT} isn't Ready")
        return 1
    if alert_active():
        print(f"not starting: {ALERT} is already active")
        return 1
    if policy_exists():
        print(f"not starting: NetworkPolicy {NAMESPACE}/{POLICY} is left from a drill that died")
        return 1
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"drill: {started}, killing {NAMESPACE}/{DEPLOYMENT}'s pod, scrape cut", flush=True)
    run = drill()
    print(report(run))
    problems = verdict(run)
    for problem in problems:
        print(f"FAIL: {problem}")
    print("PASS" if not problems else "FAIL")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
