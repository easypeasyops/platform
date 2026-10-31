"""The service chart's database (database.cluster, templates/database.yaml, platform plan 4.2)
agrees with the platform it runs on. The chart's defaults name things the platform provides
in other files; a change on either side alone would render fine and fail in the cluster:
  - its Postgres image is one the admission policy admits in the app namespaces;
  - its bucket keys come from a ClusterSecretStore that exists and may hand out that Secret.
"""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CLUSTER = yaml.safe_load((ROOT / "charts/service/values.yaml").read_text())["database"]["cluster"]


def _docs(path: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all((ROOT / path).read_text()) if d]


def _allowed_images() -> list[str]:
    (policy,) = _docs("deploy/policies/allowed-images.yaml")
    (allowed,) = [v for v in policy["spec"]["variables"] if v["name"] == "allowed"]
    # A CEL list of single-quoted regexes, backslashes doubled.
    return [r.replace("\\\\", "\\") for r in re.findall(r"'([^']+)'", allowed["expression"])]


def test_the_default_postgres_image_is_admitted() -> None:
    image = CLUSTER["imageName"]
    assert re.fullmatch(r".+@sha256:[0-9a-f]{64}", image), "pin the default by digest"
    assert any(re.search(r, image) for r in _allowed_images()), image


def test_the_backup_keys_come_from_a_store_that_hands_them_out() -> None:
    backup = CLUSTER["backup"]
    docs = _docs("deploy/secrets/bucket-store.yaml")
    stores = {d["metadata"]["name"] for d in docs if d["kind"] == "ClusterSecretStore"}
    assert backup["secretStore"] in stores
    (role,) = [d for d in docs if d["kind"] == "Role"]
    assert role["rules"][0]["resourceNames"] == [backup["remoteSecret"]]


def _golden(app: str, env: str) -> list[dict[str, Any]]:
    return _docs(f"charts/service/tests/{app}/{env}.golden.yaml")


def _wave(doc: dict[str, Any]) -> int:
    annotations = doc["metadata"].get("annotations") or {}
    return int(annotations.get("argocd.argoproj.io/sync-wave", "0"))


def test_with_its_own_database_the_migration_runs_after_it_and_before_the_app() -> None:
    """The database is created by the same sync, so the migration can't be a PreSync hook
    (it would wait for a database that doesn't exist yet): Argo CD syncs the database,
    holds the wave until the Cluster is healthy, migrates, then rolls out the app."""
    docs = _golden("notes", "staging")
    (job,) = [d for d in docs if d["kind"] == "Job"]
    annotations = job["metadata"]["annotations"]
    assert annotations["argocd.argoproj.io/hook"] == "Sync"  # overrides the Helm hook
    assert annotations["helm.sh/hook"] == "post-install,pre-upgrade"  # plain Helm
    database = [d for d in docs if d["kind"] in ("Cluster", "ObjectStore", "ExternalSecret")]
    app = [d for d in docs if d["kind"] in ("Deployment", "Rollout")]
    policies = [d for d in docs if d["kind"] == "NetworkPolicy"]
    assert max(map(_wave, database + policies)) < _wave(job) < min(map(_wave, app))


def test_without_one_the_migration_is_still_a_pre_install_hook() -> None:
    (job,) = [d for d in _golden("shortly", "prod") if d["kind"] == "Job"]
    annotations = job["metadata"]["annotations"]
    assert "argocd.argoproj.io/hook" not in annotations
    assert annotations["helm.sh/hook"] == "pre-install,pre-upgrade"
