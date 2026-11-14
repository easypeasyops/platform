"""The secret store (deploy/secrets/store.yaml, platform plan 4.1).

The store is a namespace whose Secrets are the source of every platform secret; External
Secrets copies them to where they are used. These checks keep it a store and not a leak:
  - the identity External Secrets reads it with may read Secrets there and nowhere else;
  - the ClusterSecretStore reads only that namespace, and only for the namespaces it names;
  - each value is generated once and replaced only by deleting it, and the consumers read
    keys their generator makes (or, for the few made outside the cluster, keys the runbook
    says to load);
  - cluster.sh makes none of them.
"""

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
STORE_NS = "secret-store"
# Store values no generator can make (a Slack webhook), loaded by hand: store secret -> the
# keys docs/RUNBOOK.md tells the operator to load.
LOADED = {
    "alertmanager-slack": [
        "aipostit-prod-webhook-url",
        "aipostit-staging-webhook-url",
        "platform-webhook-url",
    ]
}


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
        # Written on the first sync only: a resync must not rotate a password everything
        # already uses.
        assert spec["refreshPolicy"] == "CreatedOnce", name
        # And replaced only by deleting it: a recreated ExternalSecret fails loudly
        # instead of rotating (its first sync writes even over an existing Secret).
        assert spec["target"]["immutable"] is True, name
        # Deleting the ExternalSecret must not delete the value.
        assert spec["target"]["creationPolicy"] == "Orphan", name
        # Metadata set explicitly: Argo CD's tracking annotation isn't copied onto it.
        assert spec["target"]["template"]["metadata"]["annotations"] == {}, name


def test_consumers_read_keys_the_store_has() -> None:
    made = _generated() | LOADED
    store = _docs()["ClusterSecretStore"]
    allowed = set(store["spec"]["conditions"][0]["namespaces"])
    consumers = [
        d
        for f in ("monitoring", "alertmanager-slack", "backups")
        for d in _all(f"deploy/secrets/{f}.yaml")
    ]
    assert {c["metadata"]["namespace"] for c in consumers} == allowed
    for es in consumers:
        name = es["metadata"]["name"]
        assert es["spec"]["secretStoreRef"] == {"kind": "ClusterSecretStore", "name": "platform"}
        # Synced before the workload that reads it (same Application, an earlier wave).
        assert es["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "-1", name
        assert es["spec"]["target"]["template"]["metadata"]["annotations"] == {}, name
        for item in es["spec"]["data"]:
            ref = item["remoteRef"]
            assert ref["property"] in made[ref["key"]], (name, ref)
            # The template uses every value it reads.
            assert any(
                f".{item['secretKey']} " in v
                for v in es["spec"]["target"]["template"]["data"].values()
            ), (name, item["secretKey"])


def test_cluster_sh_makes_no_platform_secret() -> None:
    """The store's generators make them (platform plan 4.1b); cluster.sh only reads one
    (the bucket's keys, copied into each app namespace until 4.3)."""
    script = (ROOT / "scripts/cluster.sh").read_text()
    for name in _generated():
        assert f"create secret generic {name}" not in script, name


def test_the_bucket_store_hands_out_the_bucket_keys_only() -> None:
    """The apps' databases read the bucket's keys through a store of their own, whose reader
    may get that one Secret: an app namespace can't read the rest of the platform's store."""
    docs = {d["kind"]: d for d in _all("deploy/secrets/bucket-store.yaml")}
    assert docs["Role"]["metadata"]["namespace"] == STORE_NS
    assert docs["Role"]["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": ["seaweedfs-s3"],
            "verbs": ["get"],
        }
    ]
    assert "seaweedfs-s3" in _generated()
    account = docs["ServiceAccount"]["metadata"]["name"]
    assert docs["RoleBinding"]["subjects"] == [
        {"kind": "ServiceAccount", "name": account, "namespace": STORE_NS}
    ]
    store = docs["ClusterSecretStore"]["spec"]
    assert store["provider"]["kubernetes"]["auth"]["serviceAccount"]["name"] == account
    # App namespaces only: those the apps ApplicationSet labels with their app (5.7). The
    # platform's own (monitoring, backups) carry no such label.
    (condition,) = store["conditions"]
    app_label = {"key": "easypeasyops.github.io/app", "operator": "Exists"}
    assert condition == {"namespaceSelector": {"matchExpressions": [app_label]}}


def test_each_app_store_hands_its_own_keys_to_its_own_namespaces_only() -> None:
    """An app's store (deploy/secrets/app-stores.yaml) reads one Secret, named after the app,
    and only that app's namespaces may use it: one app can't read another's keys."""
    docs = _all("deploy/secrets/app-stores.yaml")
    stores = [d for d in docs if d["kind"] == "ClusterSecretStore"]
    assert stores
    by_kind_name = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    for store in stores:
        app = store["metadata"]["name"]
        reader = f"{app}-reader"
        account = store["spec"]["provider"]["kubernetes"]["auth"]["serviceAccount"]
        assert account == {"name": reader, "namespace": STORE_NS}
        assert store["spec"]["provider"]["kubernetes"]["remoteNamespace"] == STORE_NS
        role = by_kind_name[("Role", reader)]
        assert role["rules"] == [
            {"apiGroups": [""], "resources": ["secrets"], "resourceNames": [app], "verbs": ["get"]}
        ]
        binding = by_kind_name[("RoleBinding", reader)]
        assert binding["roleRef"]["name"] == reader
        assert binding["subjects"] == [
            {"kind": "ServiceAccount", "name": reader, "namespace": STORE_NS}
        ]
        assert by_kind_name[("ServiceAccount", reader)]["automountServiceAccountToken"] is False
        (condition,) = store["spec"]["conditions"]
        selector = {"matchLabels": {"easypeasyops.github.io/app": app}}
        assert condition == {"namespaceSelector": selector}
    # The secret-store Application applies the file.
    apps = (ROOT / "deploy/argocd/platform/templates/applications.yaml").read_text()
    assert "app-stores.yaml" in apps


def test_loaded_values_are_in_the_runbook() -> None:
    """Each key loaded by hand is named in the runbook's loading steps, and not generated."""
    runbook = (ROOT / "docs/RUNBOOK.md").read_text()
    for name, keys in LOADED.items():
        assert name not in _generated(), name
        assert f"create secret generic {name}" in runbook, name
        for key in keys:
            assert f"{key}=" in runbook, key
