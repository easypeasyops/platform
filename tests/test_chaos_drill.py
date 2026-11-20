"""The chaos drill (PT-110; deploy/platform/chaos-drill/): its verdict, and what it may do."""

import importlib.util
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "deploy/platform/chaos-drill"
_spec = importlib.util.spec_from_file_location("drill", DIR / "drill.py")
assert _spec is not None
assert _spec.loader is not None
drill: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drill)


def test_a_drill_passes_when_the_alert_fired_and_both_came_back() -> None:
    run = drill.Run(ready=12.0, fired=650.0, restored=650.0, resolved=700.0)
    assert drill.verdict(run) == []


def test_a_drill_whose_alert_never_fired_fails() -> None:
    run = drill.Run(ready=12.0, fired=None, restored=900.0, resolved=None)
    (problem,) = drill.verdict(run)
    assert "ExternalSecretsDown didn't reach Alertmanager" in problem


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        ({"ready": None, "fired": 650.0, "resolved": 700.0}, "wasn't Ready"),
        ({"ready": 12.0, "fired": 650.0, "resolved": None}, "didn't resolve"),
    ],
)
def test_a_drill_fails_when_a_step_misses_its_deadline(run: dict[str, Any], expected: str) -> None:
    problems = drill.verdict(drill.Run(**run))
    assert len(problems) == 1
    assert expected in problems[0]


class _Cluster:
    """The cluster and Alertmanager as the drill sees them, with a clock that jumps."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, fires: bool, kill_fails: bool = False):
        self.now = 0.0
        self.calls: list[str] = []
        self.cut = False
        self.fires = fires
        self.kill_fails = kill_fails
        for name in ("cut_scrape", "restore_scrape", "kill_pods", "ready", "alert_active"):
            monkeypatch.setattr(drill, name, getattr(self, name))
        monkeypatch.setattr(drill.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(drill.time, "sleep", self.sleep)

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def cut_scrape(self) -> None:
        self.calls.append("cut")
        self.cut = True
        self.cut_at = self.now

    def restore_scrape(self) -> None:
        self.calls.append("restore")
        self.cut = False

    def kill_pods(self) -> None:
        self.calls.append("kill")
        if self.kill_fails:
            raise RuntimeError("the API server said no")

    def ready(self) -> bool:
        return self.now >= 10

    def alert_active(self) -> bool:  # the rule's 10 minutes, and a minute on top
        return self.fires and self.cut and self.now - self.cut_at >= 660


def test_the_drill_fires_the_alert_then_lets_the_scrape_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = _Cluster(monkeypatch, fires=True)
    run = drill.drill()
    assert cluster.calls == ["cut", "kill", "restore"]
    assert drill.verdict(run) == []
    assert run.ready == 10
    assert run.fired == 660
    assert run.resolved == run.restored == 660


def test_a_drill_whose_alert_doesnt_fire_fails_and_lets_the_scrape_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = _Cluster(monkeypatch, fires=False)
    run = drill.drill()
    assert cluster.calls == ["cut", "kill", "restore"]
    assert run.fired is None
    assert drill.verdict(run)
    assert not cluster.cut


def test_a_drill_that_breaks_still_lets_the_scrape_back(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _Cluster(monkeypatch, fires=True, kill_fails=True)
    with pytest.raises(RuntimeError):
        drill.drill()
    assert cluster.calls == ["cut", "kill", "restore"]


def test_the_drill_holds_the_component_down_for_longer_than_the_alert_needs() -> None:
    """The alert needs the controller unscraped for its `for:`; the drill must wait longer,
    for the scrape interval and the evaluation on top."""
    rules = yaml.safe_load(
        (ROOT / "deploy/argocd/platform/files/external-secrets-rules.yaml").read_text()
    )
    (rule,) = [r for g in rules["groups"] for r in g["rules"] if r.get("alert") == drill.ALERT]
    assert rule["for"] == "10m"
    assert "external-secrets-metrics" in rule["expr"]
    assert drill.FIRE_WITHIN >= 10 * 60 + 120


def _manifests() -> dict[tuple[str, str], dict[str, Any]]:
    out = subprocess.run(
        ["kubectl", "kustomize", str(DIR)], check=True, capture_output=True, text=True
    ).stdout
    return {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(out)}


def test_the_drill_may_touch_only_the_controller() -> None:
    docs = _manifests()
    role = docs[("Role", "chaos-drill")]
    assert role["metadata"]["namespace"] == drill.NAMESPACE
    assert role["rules"] == [
        {
            "apiGroups": ["apps"],
            "resources": ["deployments"],
            "resourceNames": [drill.DEPLOYMENT],
            "verbs": ["get"],
        },
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["list", "delete"]},
        {"apiGroups": ["networking.k8s.io"], "resources": ["networkpolicies"], "verbs": ["create"]},
        {
            "apiGroups": ["networking.k8s.io"],
            "resources": ["networkpolicies"],
            "resourceNames": [drill.POLICY],
            "verbs": ["get", "delete"],
        },
    ]
    # RBAC can't select pods by label, nor say what a create may hold: the policy does, for
    # the drill's account only.
    policy = docs[("ValidatingAdmissionPolicy", "chaos-drill-controller-only")]["spec"]
    (condition,) = policy["matchConditions"]
    assert "system:serviceaccount:monitoring:chaos-drill" in condition["expression"]
    pods, netpol = (v["expression"] for v in policy["validations"])
    key, value = drill.LABEL
    assert f'["{key}"] == "{value}"' in pods
    assert f'{{"{key}": "{value}"}}' in netpol
    assert f'object.metadata.name == "{drill.POLICY}"' in netpol
    assert 'object.spec.policyTypes == ["Ingress"]' in netpol
    binding = docs[("ValidatingAdmissionPolicyBinding", "chaos-drill-controller-only")]
    assert binding["spec"]["validationActions"] == ["Deny"]


def test_the_drill_runs_weekly_once_and_within_its_deadlines() -> None:
    cronjob = _manifests()[("CronJob", "chaos-drill")]["spec"]
    assert cronjob["schedule"] == "0 10 * * 1"
    assert cronjob["concurrencyPolicy"] == "Forbid"
    job = cronjob["jobTemplate"]["spec"]
    assert job["backoffLimit"] == 0
    total = drill.FIRE_WITHIN + drill.READY_WITHIN + drill.RESOLVE_WITHIN
    assert job["activeDeadlineSeconds"] > total
    pod = job["template"]["spec"]
    assert pod["serviceAccountName"] == "chaos-drill"
    (container,) = pod["containers"]
    assert "@sha256:" in container["image"]
    assert container["args"] == ["python", "-u", "/drill/drill.py"]
