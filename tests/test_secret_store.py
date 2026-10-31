"""The secret store (deploy/secrets/store.yaml, platform plan 4.1).

The store is a namespace whose Secrets are the source of every platform secret; External
Secrets copies them to where they are used. These checks keep it a store and not a leak:
  - the identity External Secrets reads it with may read Secrets there and nowhere else;
  - the ClusterSecretStore reads only that namespace, and only for the namespaces it names;
  - each value is generated once and never rotated by a resync, and the generator, the
    consumers that read it and cluster.sh's seeding agree on its keys.
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


def _all(path: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all((ROOT / path).read_text()) if d]


def _generated() -> dict[str, list[str]]:
    """Store secret name -> the keys its generator makes."""
    docs = _all("deploy/secrets/generated.yaml")
    keys = {d["metadata"]["name"]: d["spec"]["secretKeys"] for d in docs if d["kind"] == "Password"}
    made = {}
    for es in (d for d in docs if d["kind"] == "ExternalSecret"):
        (source,) = es["spec"]["dataFrom"]
        made[es["spec"]["target"]["name"]] = keys[source["sourceRef"]["generatorRef"]["name"]]
    return made


def test_store_values_are_made_once_and_kept() -> None:
    for es in (d for d in _all("deploy/secrets/generated.yaml") if d["kind"] == "ExternalSecret"):
        name = es["metadata"]["name"]
        assert es["metadata"]["namespace"] == STORE_NS, name
        spec = es["spec"]
        # Created only when the store lacks it, never refreshed: a resync must not rotate
        # a password everything already uses, and a seeded store keeps its values.
        assert spec["refreshPolicy"] == "CreatedOnce", name
        # Deleting the ExternalSecret must not delete the value.
        assert spec["target"]["creationPolicy"] == "Orphan", name
        # Metadata set explicitly: Argo CD's tracking annotation isn't copied onto it.
        assert spec["target"]["template"]["metadata"]["annotations"] == {}, name


def test_consumers_read_keys_the_store_has() -> None:
    made = _generated()
    store = _docs()["ClusterSecretStore"]
    allowed = set(store["spec"]["conditions"][0]["namespaces"])
    consumers = [d for f in ("monitoring", "backups") for d in _all(f"deploy/secrets/{f}.yaml")]
    assert {c["metadata"]["namespace"] for c in consumers} == allowed
    for es in consumers:
        name = es["metadata"]["name"]
        assert es["spec"]["secretStoreRef"] == {"kind": "ClusterSecretStore", "name": "platform"}
        assert es["spec"]["target"]["template"]["metadata"]["annotations"] == {}, name
        for item in es["spec"]["data"]:
            ref = item["remoteRef"]
            assert ref["property"] in made[ref["key"]], (name, ref)
            # The template uses every value it reads.
            assert any(
                f".{item['secretKey']} " in v
                for v in es["spec"]["target"]["template"]["data"].values()
            ), (name, item["secretKey"])


def test_cluster_sh_seeds_exactly_what_the_generators_make() -> None:
    """Seeding an older cluster's store copies the source keys the generators would make:
    a missing key would be generated anew, an extra one would be derived data."""
    script = (ROOT / "scripts/cluster.sh").read_text()
    calls = re.findall(r"^\s*store_secret (\S+) (\S+) (.+)$", script, re.M)
    assert {name: keys.split() for _, name, keys in calls} == _generated()
    consumers = {
        d["metadata"]["name"]: d["metadata"]["namespace"]
        for f in ("monitoring", "backups")
        for d in _all(f"deploy/secrets/{f}.yaml")
    }
    assert {name: ns for ns, name, _ in calls} == consumers


def _store_secret(tmp_path: Path, source_exists: bool) -> Path:
    """Run cluster.sh's store_secret against a stub kubectl; return where it wrote."""
    bin_dir, created = tmp_path / "bin", tmp_path / "created.json"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    source = json.dumps(
        {
            "metadata": {"name": "grafana-admin", "namespace": "monitoring", "uid": "u"},
            "data": {"admin-user": "YQ==", "admin-password": "cA=="},
        }
    )
    found = f"echo '{source}'" if source_exists else "exit 1"
    kubectl.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  "create namespace"*) echo "apiVersion: v1" ;;\n'
        '  "apply -f -") cat >/dev/null ;;\n'
        '  "-n secret-store get secret"*) exit 1 ;;  # not in the store yet\n'
        f'  "-n monitoring get secret"*) {found} ;;\n'
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
    return created


def test_store_secret_copies_only_the_named_keys(tmp_path: Path) -> None:
    secret = json.loads(_store_secret(tmp_path, source_exists=True).read_text())
    assert secret["metadata"] == {"name": "grafana-admin", "namespace": STORE_NS}
    assert secret["data"] == {"admin-password": "cA=="}


def test_store_secret_does_nothing_on_a_new_cluster(tmp_path: Path) -> None:
    """No old Secret to copy: the store's generators make the value."""
    assert not _store_secret(tmp_path, source_exists=False).exists()
