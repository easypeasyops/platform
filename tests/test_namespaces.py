"""Environments and their namespaces (platform plan, Phase 2: a namespace per app and env).

scripts/lib/namespaces.sh is the one place that says which namespace an environment runs in;
every script asks it instead of writing `-n prod`, so the switch to <app>-<env> namespaces
is one setting.
"""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "scripts/lib/namespaces.sh"


def _mapping(prefix: str | None) -> str:
    env = {"PATH": "/usr/bin:/bin"} | ({} if prefix is None else {"NAMESPACE_PREFIX": prefix})
    cmd = f'source "{LIB}"; echo "${{ENVS[*]}}|$STAGING_NS|$PROD_NS|$(ns_of prod)"'
    return subprocess.run(
        ["bash", "-c", cmd], env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.mark.parametrize(
    ("prefix", "want"),
    [
        (None, "staging prod|shortly-staging|shortly-prod|shortly-prod"),  # <app>-<env>
    ],
)
def test_environments_map_to_namespaces(prefix: str | None, want: str) -> None:
    assert _mapping(prefix) == want


def test_scripts_name_no_namespace_directly() -> None:
    """`kubectl -n prod` in a script would miss the switch to <app>-<env> namespaces."""
    literal = re.compile(r"(-n|--namespace)[ =](\"|')?(staging|prod)\b")
    for path in sorted((ROOT / "scripts").glob("*.sh")):
        hits = [line for line in path.read_text().splitlines() if literal.search(line)]
        assert not hits, (path.name, hits)


APP_NAMESPACES = {"matchExpressions": [{"key": "easypeasyops.github.io/app", "operator": "Exists"}]}


def test_project_and_backups_admit_the_apps_namespaces() -> None:
    """An app's project names its own namespaces, <app>-<env> (platform plan, Phase 2); what
    serves every app selects their namespaces by the label the ApplicationSet gives them
    (5.7)."""
    want = {"shortly-staging", "shortly-prod"}
    project = yaml.safe_load((ROOT / "apps/shortly/project.yaml").read_text())
    assert {d["namespace"] for d in project["spec"]["destinations"]} == want
    docs = yaml.safe_load_all((ROOT / "deploy/kind/backups.yaml").read_text())
    (bucket_policy,) = [d for d in docs if d and d["kind"] == "NetworkPolicy"]
    # The CloudNativePG pods (their Barman Cloud sidecar) of every app, and the restore drill's.
    peers = bucket_policy["spec"]["ingress"][0]["from"]
    cnpg = {"matchExpressions": [{"key": "cnpg.io/cluster", "operator": "Exists"}]}
    drill = {"key": "kubernetes.io/metadata.name", "operator": "In", "values": ["restore-drill"]}
    assert peers == [
        {"namespaceSelector": APP_NAMESPACES, "podSelector": cnpg},
        {"namespaceSelector": {"matchExpressions": [drill]}, "podSelector": cnpg},
    ]
    bucket = yaml.safe_load_all((ROOT / "deploy/secrets/bucket-store.yaml").read_text())
    (store,) = [d for d in bucket if d["kind"] == "ClusterSecretStore"]
    assert store["spec"]["conditions"] == [{"namespaceSelector": APP_NAMESPACES}]
    monitoring = yaml.safe_load((ROOT / "deploy/kind/monitoring-values.yaml").read_text())
    routes = monitoring["alertmanager"]["config"]["route"]["routes"]
    assert ['namespace =~ ".+-staging"'] in [r["matchers"] for r in routes]


def test_argo_cd_creates_the_apps_namespaces_with_their_baseline() -> None:
    """Each app's Application creates its namespace with the restricted Pod Security labels
    and applies the platform's baseline (default-deny); the app's project lets it create
    exactly its own namespaces."""
    appset = yaml.safe_load((ROOT / "apps/applicationset.yaml").read_text())["spec"]
    policy = appset["template"]["spec"]["syncPolicy"]
    assert "CreateNamespace=true" in policy["syncOptions"]
    labels = policy["managedNamespaceMetadata"]["labels"]
    assert labels == {
        "pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/warn": "restricted",
    }
    # Argo CD alone makes them: cluster.sh only bootstraps Argo CD (platform plan 4.3).
    cluster_sh = (ROOT / "scripts/cluster.sh").read_text()
    assert "ns_of" not in cluster_sh  # it touches no app namespace
    assert "deploy/app-namespace/default-deny.yaml" not in cluster_sh

    baseline = "deploy/app-namespace"
    assert f"path: {baseline}\n" in appset["templatePatch"]
    (deny,) = yaml.safe_load_all((ROOT / baseline / "default-deny.yaml").read_text())
    assert deny["kind"] == "NetworkPolicy"
    assert deny["spec"] == {"podSelector": {}, "policyTypes": ["Ingress"]}

    project = yaml.safe_load((ROOT / "apps/shortly/project.yaml").read_text())["spec"]
    assert "https://github.com/easypeasyops/platform.git" in project["sourceRepos"]
    namespaces = {d["namespace"] for d in project["destinations"]}
    whitelist = project["clusterResourceWhitelist"]
    assert all((r["group"], r["kind"]) == ("", "Namespace") for r in whitelist), whitelist
    assert {r["name"] for r in whitelist} == namespaces


@pytest.mark.parametrize("project_file", sorted(ROOT.glob("apps/*/project.yaml")), ids=str)
def test_each_apps_project_deploys_to_its_own_namespaces_only(project_file: Path) -> None:
    """apps/<app>/project.yaml: the project named after its folder, its two namespaces
    <app>-<env> and nothing else, the platform's chart and baseline among its sources."""
    app = project_file.parent.name
    project = yaml.safe_load(project_file.read_text())
    assert project["metadata"] == {"name": app, "namespace": "argocd"}
    spec = project["spec"]
    want = {f"{app}-staging", f"{app}-prod"}
    assert {d["namespace"] for d in spec["destinations"]} == want
    assert {d["server"] for d in spec["destinations"]} == {"https://kubernetes.default.svc"}
    assert spec["clusterResourceWhitelist"] == [
        {"group": "", "kind": "Namespace", "name": ns} for ns in sorted(want, reverse=True)
    ]
    assert "oci://ghcr.io/easypeasyops/charts/service" in spec["sourceRepos"]
    assert "https://github.com/easypeasyops/platform.git" in spec["sourceRepos"]
