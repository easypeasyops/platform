"""The secret store (deploy/secrets/store.yaml, platform plan 4.1).

The store is a namespace whose Secrets are the source of every platform secret; External
Secrets copies them to where they are used. These checks keep it a store and not a leak:
  - the identity External Secrets reads it with may read Secrets there and nowhere else;
  - the ClusterSecretStore reads only that namespace, and only for the namespaces it names;
  - cluster.sh copies into the store exactly the keys the store is meant to hold.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
STORE_NS = "secret-store"


def _docs() -> dict[str, dict[str, Any]]:
    docs = yaml.safe_load_all((ROOT / "deploy/secrets/store.yaml").read_text())
    return {d["kind"]: d for d in docs if d}


def test_the_store_identity_reads_secrets_in_the_store_only() -> None:
    docs = _docs()
    assert "ClusterRole" not in docs
    assert "ClusterRoleBinding" not in docs
    role, binding = docs["Role"], docs["RoleBinding"]
    assert role["metadata"]["namespace"] == STORE_NS
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get", "list"]}
    ]
    assert binding["metadata"]["namespace"] == STORE_NS
    assert binding["roleRef"]["name"] == role["metadata"]["name"]
    account = docs["ServiceAccount"]
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": account["metadata"]["name"], "namespace": STORE_NS}
    ]
    assert account["automountServiceAccountToken"] is False


def test_the_cluster_store_reads_the_store_for_named_namespaces_only() -> None:
    store = _docs()["ClusterSecretStore"]["spec"]
    kubernetes = store["provider"]["kubernetes"]
    assert kubernetes["remoteNamespace"] == STORE_NS
    assert kubernetes["auth"]["serviceAccount"]["namespace"] == STORE_NS
    # Without conditions any namespace could read every platform secret.
    (condition,) = store["conditions"]
    assert set(condition) == {"namespaces"}
    assert STORE_NS not in condition["namespaces"]
    project = yaml.safe_load((ROOT / "deploy/argocd/platform-project.yaml").read_text())
    platform_namespaces = {d["namespace"] for d in project["spec"]["destinations"]}
    assert set(condition["namespaces"]) <= platform_namespaces


def test_cluster_sh_stores_only_source_values() -> None:
    """What the store holds per secret; anything derived is the consumer's template."""
    script = (ROOT / "scripts/cluster.sh").read_text()
    calls = re.findall(r"^\s*store_secret (\S+) (\S+) (.+)$", script, re.M)
    assert {(ns, name): keys.split() for ns, name, keys in calls} == {
        ("monitoring", "grafana-admin"): ["admin-password"],
        ("backups", "seaweedfs-s3"): ["access-key", "secret-key"],
    }


def test_store_secret_copies_only_the_named_keys(tmp_path: Path) -> None:
    """Run store_secret with a stub kubectl: the store's copy has the listed keys only."""
    bin_dir, created = tmp_path / "bin", tmp_path / "created.json"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  "create namespace"*) echo "apiVersion: v1" ;;\n'
        '  "apply -f -") cat >/dev/null ;;\n'
        '  "-n secret-store get secret"*) exit 1 ;;  # not in the store yet\n'
        '  "-n monitoring get secret"*) echo \'{"metadata": {"name": "grafana-admin",'
        ' "namespace": "monitoring", "uid": "u"}, "data": {"admin-user": "YQ==",'
        ' "admin-password": "cA=="}}\' ;;\n'
        f'  "create -f -") cat >"{created}" ;;\n'
        '  *) echo "unexpected: kubectl $*" >&2; exit 2 ;;\n'
        "esac\n"
    )
    kubectl.chmod(0o755)
    script = (ROOT / "scripts/cluster.sh").read_text()
    # Just the function: the script's top level needs a cluster and its arguments.
    start = script.index("store_secret() {")
    function = script[start : script.index("\n}\n", start) + 3]
    subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail\n{function}\nstore_secret monitoring grafana-admin admin-password",
        ],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        check=True,
        capture_output=True,
        text=True,
    )
    secret = json.loads(created.read_text())
    assert secret["metadata"] == {"name": "grafana-admin", "namespace": STORE_NS}
    assert secret["data"] == {"admin-password": "cA=="}
