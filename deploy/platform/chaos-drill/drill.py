"""The chaos drill (PT-110; docs/RUNBOOK.md, The chaos drill).

Takes one platform component away, checks its alert reaches Alertmanager, lets the
component come back, and checks it is Ready again and the alert resolved, each within a
deadline. Prints the times; exits 1 if any step missed its deadline.

The component is the External Secrets controller: while it is down nothing is refreshed,
but every Secret keeps its value and nothing that runs notices. Its alert,
ExternalSecretsDown, needs it unscraped for 10 minutes, and the Deployment replaces a
deleted pod in seconds: so the drill holds it down, deleting every pod of the Deployment
as soon as it appears, until the alert fires. Argo CD sees nothing to undo (the
Deployment is untouched), and if the drill dies the Deployment brings the pod back.

Runs in the cluster (the CronJob beside this file), with the standard library only.
"""

import json
import os
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
SELECTOR = "app.kubernetes.io/name=external-secrets"  # the controller's pods, only
ALERT = "ExternalSecretsDown"  # for: 10m (deploy/argocd/platform/files/external-secrets-rules.yaml)
ALERTMANAGER = "http://kps-alertmanager.monitoring.svc:9093"
API = "https://kubernetes.default.svc"
SA = "/var/run/secrets/kubernetes.io/serviceaccount"

# Deadlines, in seconds. Firing: the rule's 10 minutes, plus the 30 s scrape, the 15 s
# evaluation and Alertmanager's receipt, with room. Overridable to show a failing drill.
FIRE_WITHIN = int(os.environ.get("DRILL_FIRE_WITHIN", "900"))
READY_WITHIN = 300  # the pod scheduled, started and Ready
RESOLVE_WITHIN = 300  # scraped again (30 s), evaluated (15 s), resolved in Alertmanager


@dataclass
class Run:
    """Seconds from the drill's start; None: it didn't happen before its deadline."""

    fired: float | None = None
    stopped: float | None = None  # when the drill stopped deleting
    ready: float | None = None
    resolved: float | None = None
    deleted: int = 0


def verdict(run: Run) -> list[str]:
    """What went wrong; empty when the drill passed."""
    problems = []
    if run.fired is None:
        problems.append(f"{ALERT} didn't reach Alertmanager within {FIRE_WITHIN}s of the outage")
    if run.ready is None:
        problems.append(f"{NAMESPACE}/{DEPLOYMENT} wasn't Ready within {READY_WITHIN}s")
    if run.fired is not None and run.ready is not None and run.resolved is None:
        problems.append(f"{ALERT} didn't resolve within {RESOLVE_WITHIN}s of the component Ready")
    return problems


def report(run: Run) -> str:
    def at(t: float | None) -> str:
        return "never" if t is None else f"{t:.0f}s"

    return (
        f"{ALERT}: fired at {at(run.fired)}, drill stopped at {at(run.stopped)} "
        f"({run.deleted} pods deleted), {DEPLOYMENT} Ready at {at(run.ready)}, "
        f"alert resolved at {at(run.resolved)} (seconds from the first delete)"
    )


def _kube(method: str, path: str, timeout: float = 30) -> Any:
    with open(f"{SA}/token") as f:
        token = f.read()
    request = urllib.request.Request(  # noqa: S310 (the API server's fixed https URL)
        API + path, method=method, headers={"Authorization": f"Bearer {token}"}
    )
    context = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    return urllib.request.urlopen(request, timeout=timeout, context=context)  # noqa: S310


def alert_active() -> bool:
    query = urllib.parse.urlencode({"filter": f'alertname="{ALERT}"'})
    url = f"{ALERTMANAGER}/api/v2/alerts?{query}"
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 (fixed http URL)
        return bool(json.load(response))


def ready() -> bool:
    with _kube("GET", f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{DEPLOYMENT}") as r:
        d = json.load(r)
    want = d["spec"].get("replicas", 1)
    return bool(want >= 1 and d["status"].get("readyReplicas", 0) >= want)


def delete_pods_for(seconds: int) -> int:
    """Watch the controller's pods for a while, deleting each as it appears (the watch
    starts with the pods there are). Returns how many it deleted."""
    query = urllib.parse.urlencode(
        {"labelSelector": SELECTOR, "watch": "1", "timeoutSeconds": str(seconds)}
    )
    deleted: set[str] = set()
    with _kube("GET", f"/api/v1/namespaces/{NAMESPACE}/pods?{query}", seconds + 30) as stream:
        for line in stream:
            event = json.loads(line)
            meta = event["object"]["metadata"]
            if (
                event["type"] in ("ADDED", "MODIFIED")
                and "deletionTimestamp" not in meta
                and meta["name"] not in deleted
            ):
                try:
                    _kube("DELETE", f"/api/v1/namespaces/{NAMESPACE}/pods/{meta['name']}").close()
                    deleted.add(meta["name"])
                except urllib.error.HTTPError as e:
                    if e.code != 404:  # gone already
                        raise
    return len(deleted)


def wait_for(check: Callable[[], bool], within: int, start: float) -> float | None:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if check():
            return time.monotonic() - start
        time.sleep(5)
    return None


def drill() -> Run:
    run = Run()
    start = time.monotonic()
    try:
        while time.monotonic() - start < FIRE_WITHIN:
            run.deleted += delete_pods_for(10)
            if alert_active():
                run.fired = time.monotonic() - start
                break
    finally:  # from here on the Deployment brings its pod back
        run.stopped = time.monotonic() - start
    run.ready = wait_for(ready, READY_WITHIN, start)
    if run.fired is not None and run.ready is not None:
        run.resolved = wait_for(lambda: not alert_active(), RESOLVE_WITHIN, start)
    return run


def main() -> int:
    if not ready():
        print(f"not starting: {NAMESPACE}/{DEPLOYMENT} isn't Ready")
        return 1
    if alert_active():
        print(f"not starting: {ALERT} is already active")
        return 1
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(
        f"drill: {started}, holding {NAMESPACE}/{DEPLOYMENT} down until {ALERT} fires", flush=True
    )
    run = drill()
    print(report(run))
    problems = verdict(run)
    for problem in problems:
        print(f"FAIL: {problem}")
    print("PASS" if not problems else "FAIL")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
