"""The dashboards' deploy marks (PT-28): a Grafana annotation at the minute an Argo CD
Application's last sync deployed a new revision, naming the Application and the revision.

Argo CD's own metrics carry no revision, so kube-state-metrics reads it from each
Application's status (deploy/kind/monitoring-values.yaml): a new revision is a new series,
and the mark is the minutes in which one is new. promtool checks the query on series shaped
as kube-state-metrics gives them: a mark for a new revision, and none for a sync that hides
the series for a moment, a laptop asleep, or an Application's first sync.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.test_alerting import PROMETHEUS_IMAGE, _docker

ROOT = Path(__file__).resolve().parent.parent
FRONT_DOOR = ROOT / "deploy/argocd/platform/files/front-door-dashboard.json"
SERIES = ("argocd_application_synced_revision_info", "argocd_application_synced_revisions_info")


def _deploys(dashboard: Path) -> dict[str, Any]:
    annotations: list[dict[str, Any]] = json.loads(dashboard.read_text())["annotations"]["list"]
    (annotation,) = [a for a in annotations if a["name"] == "Deploys"]
    return annotation


def test_kube_state_metrics_exports_what_each_application_deployed() -> None:
    ksm = yaml.safe_load((ROOT / "deploy/kind/monitoring-values.yaml").read_text())[
        "kube-state-metrics"
    ]
    assert {
        "apiGroups": ["argoproj.io"],
        "resources": ["applications"],
        "verbs": ["list", "watch"],
    } in ksm["rbac"]["extraRules"]
    (resource,) = [
        r
        for r in ksm["customResourceState"]["config"]["spec"]["resources"]
        if r["groupVersionKind"]
        == {"group": "argoproj.io", "version": "v1alpha1", "kind": "Application"}
    ]
    assert resource["labelsFromPath"] == {
        "name": ["metadata", "name"],
        "project": ["spec", "project"],
    }
    # One source: syncResult.revision; several: syncResult.revisions, a series for each.
    paths = {
        f"{resource['metricNamePrefix']}_{m['name']}": (m["each"]["type"], m["each"]["info"])
        for m in resource["metrics"]
    }
    sync_result = ["status", "operationState", "syncResult"]
    assert paths == {
        SERIES[0]: (
            "Info",
            {"path": [*sync_result, "revision"], "labelsFromPath": {"revision": []}},
        ),
        SERIES[1]: (
            "Info",
            {"path": [*sync_result, "revisions"], "labelsFromPath": {"revision": []}},
        ),
    }


def test_the_front_door_marks_every_applications_deploys() -> None:
    annotation = _deploys(FRONT_DOOR)
    assert annotation["enable"] is True
    assert annotation["datasource"] == {"type": "prometheus", "uid": "${datasource}"}
    # Every Application's, not a namespace's: a platform merge can move any of them.
    selectors = re.findall(r"\{__name__[^}]*\}", annotation["expr"])
    assert selectors
    for selector in selectors:
        assert (
            selector
            == '{__name__=~"argocd_application_synced_revisions?_info",job="kube-state-metrics"}'
        )
    assert "{{name}}" in annotation["titleFormat"]
    assert annotation["textFormat"] == "{{revision}}"
    runbook = (ROOT / "docs/RUNBOOK.md").read_text()
    assert "**Deploy marks:**" in runbook
    assert SERIES[1] in runbook


def _series(name: str, app: str, revision: str) -> str:
    # As kube-state-metrics gives them, with its own pod's namespace.
    return (
        f'{name}{{job="kube-state-metrics",namespace="monitoring",name="{app}",'
        f'project="{app.split("-")[0]}",revision="{revision}"}}'
    )


@pytest.mark.integration
@pytest.mark.parametrize("dashboard", [FRONT_DOOR], ids=lambda p: p.stem)
def test_a_new_revision_is_marked_once_and_nothing_else_is(dashboard: Path, tmp_path: Path) -> None:
    _docker()
    commit = {c: c * 40 for c in "abef"}
    chart = {c: "sha256:" + c * 64 for c in "cd"}
    one, several = SERIES
    input_series = {
        # The platform's monitoring, one source: a merge moves it from a to b at 20m.
        _series(one, "monitoring-x", commit["a"]): "1x19",
        _series(one, "monitoring-x", commit["b"]): "_x20 1x20",
        # aipostit's prod, three sources: its chart pin moves at 30m. The sync before
        # (25m) has no result while it runs: the series are gone for a scrape.
        _series(several, "aipostit-prod", chart["c"]): "1x24 _ 1x4",
        _series(several, "aipostit-prod", chart["d"]): "_x30 1x10",
        _series(several, "aipostit-prod", commit["e"]): "1x24 _ 1x15",
        # A new Application's first sync, and one whose series stop for 20 minutes (the
        # laptop asleep): neither is a deploy to mark.
        _series(one, "new-x", commit["a"]): "_x10 1x30",
        _series(one, "sleepy-x", commit["f"]): "1x5 _x20 1x15",
    }

    def at(minute: int, *marks: tuple[str, str, str]) -> dict[str, Any]:
        return {
            "expr": _deploys(dashboard)["expr"],
            "eval_time": f"{minute}m",
            "exp_samples": [{"labels": _series(*m), "value": 1} for m in marks],
        }

    monitoring = (one, "monitoring-x", "bbbbbbb")  # the commit's short SHA
    pin = (several, "aipostit-prod", "sha256:dddddddddddd")  # the digest's start
    scenario = {
        "evaluation_interval": "1m",
        "tests": [
            {
                "interval": "1m",
                "input_series": [{"series": s, "values": v} for s, v in input_series.items()],
                "promql_expr_test": [
                    at(5),
                    at(11),  # new-x's first sync
                    at(19),
                    at(20, monitoring),
                    at(21, monitoring),
                    at(22),
                    at(25),
                    at(26),  # back after the sync, and sleepy-x awake
                    at(27),
                    at(30, pin),
                    at(31, pin),
                    at(32),
                ],
            }
        ],
    }
    (tmp_path / "deploys_test.yaml").write_text(yaml.safe_dump(scenario, sort_keys=False))
    tmp_path.chmod(0o755)  # the promtool container runs as nobody
    (tmp_path / "deploys_test.yaml").chmod(0o644)
    result = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{tmp_path}:/t:ro", "--entrypoint", "promtool",
         PROMETHEUS_IMAGE, "test", "rules", "/t/deploys_test.yaml"],
        capture_output=True,
        text=True,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
