"""The service chart's rbac.clusterRead (templates/rbac.yaml, PT-52): read access across the
cluster to the monitoring objects and to namespaces, for the apps the platform names.

An app's values live in its own repo, so the chart can't know who sets the value. What stops
an app that isn't on the list is its AppProject: Argo CD applies a cluster-scoped object only
if apps/<app>/project.yaml lists it, and refuses the whole sync otherwise. So the list holds
as long as (1) the value renders exactly this access, under names a project can list,
(2) only a listed app's project lists them, and (3) scripts/check-apps.py, which renders
every app with its values in CI, says so before Argo CD refuses.
"""

import importlib.util
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "charts/service/tests/rbac/staging.golden.yaml"  # release rbac, in rbac-staging
READ = ["get", "list", "watch"]

_spec = importlib.util.spec_from_file_location("check_apps", ROOT / "scripts/check-apps.py")
assert _spec
assert _spec.loader
check_apps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_apps)


def _docs() -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(GOLDEN.read_text()) if d]


def _one(kind: str) -> dict[str, Any]:
    (doc,) = [d for d in _docs() if d["kind"] == kind]
    return doc


def test_it_grants_read_on_monitoring_objects_and_namespaces_and_nothing_else() -> None:
    assert _one("ClusterRole")["rules"] == [
        {
            "apiGroups": ["monitoring.coreos.com"],
            "resources": ["servicemonitors", "podmonitors", "probes", "prometheusrules"],
            "verbs": READ,
        },
        {"apiGroups": [""], "resources": ["namespaces"], "verbs": READ},
    ]
    assert not [d for d in _docs() if d["kind"] in ("Role", "RoleBinding")]


def test_it_binds_the_apps_own_service_account_which_its_pods_run_as() -> None:
    account = _one("ServiceAccount")["metadata"]["name"]
    binding = _one("ClusterRoleBinding")
    assert binding["roleRef"]["name"] == _one("ClusterRole")["metadata"]["name"]
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": account, "namespace": "rbac-staging"}
    ]
    pod = _one("Deployment")["spec"]["template"]["spec"]
    assert (pod["serviceAccountName"], pod["automountServiceAccountToken"]) == (account, True)


def test_its_pods_may_reach_the_api_server_and_the_default_app_may_not() -> None:
    """The app's own NetworkPolicy gets one more egress rule: TCP 6443 to the private ranges
    (the API server is on a node's network, so by address). An app without the value has no
    such rule: the minimal fixture's egress is the rest of this one's."""
    (policy,) = [d for d in _docs() if d["kind"] == "NetworkPolicy"]
    *rest, api_server = policy["spec"]["egress"]
    assert api_server == {
        "to": [
            {"ipBlock": {"cidr": cidr}}
            for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        ],
        "ports": [{"port": 6443, "protocol": "TCP"}],
    }
    minimal = ROOT / "charts/service/tests/minimal/staging.golden.yaml"
    (default,) = [
        d for d in yaml.safe_load_all(minimal.read_text()) if d and d["kind"] == "NetworkPolicy"
    ]
    assert default["spec"]["egress"] == rest


def test_a_project_can_list_what_it_renders_by_name() -> None:
    """The names a project.yaml lists are the ones the chart renders: one pair per namespace."""
    rendered = [
        {"group": "rbac.authorization.k8s.io", "kind": d["kind"], "name": d["metadata"]["name"]}
        for d in _docs()
        if d["kind"].startswith("Cluster")
    ]
    assert rendered == check_apps.cluster_read_objects("rbac-staging")


def _check(app: str, whitelist: list[dict[str, str]]) -> list[str]:
    check_apps.errors.clear()
    project = {"clusterResourceWhitelist": whitelist}
    check_apps.check_cluster_read(f"apps/{app}/envs/staging.yaml", app, project, _docs())
    errors = list(check_apps.errors)
    check_apps.errors.clear()
    return errors


def test_an_app_off_the_list_that_sets_it_fails_check_apps() -> None:
    """Even with a project that lists the objects: the list is the review, not the project."""
    assert "deploybot" not in check_apps.CLUSTER_READ_APPS
    listed: list[dict[str, str]] = check_apps.cluster_read_objects("rbac-staging")
    for whitelist in ([], listed):
        errors = _check("deploybot", whitelist)
        assert len(errors) == 2, errors  # the ClusterRole and its binding
        assert all("isn't in CLUSTER_READ_APPS" in e for e in errors)


def test_an_app_on_the_list_needs_its_project_to_allow_it() -> None:
    assert "servicemonitor" in check_apps.CLUSTER_READ_APPS
    assert _check("servicemonitor", check_apps.cluster_read_objects("rbac-staging")) == []
    errors = _check("servicemonitor", [{"group": "", "kind": "Namespace", "name": "rbac-staging"}])
    assert len(errors) == 2, errors
    assert all("Argo CD would refuse the sync" in e for e in errors)


@pytest.mark.parametrize("project_file", sorted(ROOT.glob("apps/*/project.yaml")), ids=str)
def test_only_a_listed_apps_project_allows_cluster_scoped_objects(project_file: Path) -> None:
    """The enforcement itself: beside its namespaces, a project lists nothing cluster-scoped,
    or, for an app on the list, exactly what rbac.clusterRead renders in its namespaces."""
    app = project_file.parent.name
    spec = yaml.safe_load(project_file.read_text())["spec"]
    extra = [r for r in spec["clusterResourceWhitelist"] if r["kind"] != "Namespace"]
    want: list[dict[str, str]] = []
    if app in check_apps.CLUSTER_READ_APPS:
        for destination in spec["destinations"]:
            want += check_apps.cluster_read_objects(destination["namespace"])
    assert extra == want
    assert "clusterResourceBlacklist" not in spec
