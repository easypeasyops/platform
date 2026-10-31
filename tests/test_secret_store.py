"""The secret store (deploy/secrets/store.yaml, platform plan 4.1).

The store is a namespace whose Secrets are the source of every platform secret; External
Secrets copies them to where they are used. These checks keep it a store and not a leak:
  - the identity External Secrets reads it with may read Secrets there and nowhere else;
  - the ClusterSecretStore reads only that namespace, and only for the namespaces it names;
  - each value is generated once and replaced only by deleting it, and the consumers read
    keys their generator makes;
  - cluster.sh makes none of them.
"""

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


def test_cluster_sh_makes_no_platform_secret() -> None:
    """The store's generators make them (platform plan 4.1b); cluster.sh only reads one
    (the bucket's keys, copied into each app namespace until 4.3)."""
    script = (ROOT / "scripts/cluster.sh").read_text()
    for name in _generated():
        assert f"create secret generic {name}" not in script, name
