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
    run = drill.Run(fired=650.0, stopped=650.0, ready=665.0, resolved=700.0, deleted=40)
    assert drill.verdict(run) == []


def test_a_drill_whose_alert_never_fired_fails() -> None:
    run = drill.Run(fired=None, stopped=900.0, ready=915.0, resolved=None)
    (problem,) = drill.verdict(run)
    assert "ExternalSecretsDown didn't reach Alertmanager" in problem


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        ({"fired": 650.0, "ready": None}, "wasn't Ready"),
        ({"fired": 650.0, "ready": 665.0, "resolved": None}, "didn't resolve"),
    ],
)
def test_a_drill_fails_when_a_step_misses_its_deadline(run: dict[str, Any], expected: str) -> None:
    problems = drill.verdict(drill.Run(**run))
    assert len(problems) == 1
    assert expected in problems[0]


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


def test_the_drill_may_delete_only_the_controllers_pods() -> None:
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
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["list", "watch", "delete"]},
    ]
    # RBAC can't select pods by label: the policy does, for the drill's account only.
    policy = docs[("ValidatingAdmissionPolicy", "chaos-drill-controller-pods-only")]["spec"]
    (condition,) = policy["matchConditions"]
    assert "system:serviceaccount:monitoring:chaos-drill" in condition["expression"]
    (validation,) = policy["validations"]
    key, value = drill.SELECTOR.split("=")
    assert f'["{key}"] == "{value}"' in validation["expression"]
    binding = docs[("ValidatingAdmissionPolicyBinding", "chaos-drill-controller-pods-only")]
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
