#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml==6.0.3"]
# ///
"""Check apps/: what Argo CD will do with every env file, before it does it.

    uv run --script scripts/check-apps.py --out <dir>   # renders land in <dir>/<app>-<env>.yaml
    VERIFY=1 ...                                        # also gh attestation verify each digest

For each apps/<app>/envs/<env>.yaml (the ApplicationSet's input):
  1. it has app, env, chart.version, chart.digest, values.repoURL/revision/files, and its
     app and env match its path;
  2. apps/<app>/project.yaml exists and allows the app's namespace (<app>-<env>) as a
     destination and as the Namespace its Application creates, its values repo, the service
     chart and the platform's repo (the namespace's baseline);
  3. the chart digest is published, and is the version the file says; with VERIFY=1, the
     platform's release workflow signed it (gh attestation verify, as the file asks);
  4. the chart at that digest renders with the app's values files, fetched from its repo at
     the named revision and layered in order, as Argo CD will render them. The renders are
     written to --out for kubeconform;
  5. a render with the chart's rbac.clusterRead objects (a ClusterRole and its binding) is
     from an app in CLUSTER_READ_APPS whose project allows them by name. Argo CD refuses to
     sync any other: the project is what enforces the list, this says so before it does.
Then, across every app: 6. each hostname their HTTPRoutes serve is probed through the front
door, on the listener the route is on, and nothing else is but the tools' own routes
(appFrontDoorProbes, apps/front-door-probes.yaml; frontDoorProbes, the tools' and
servicemonitor's, deploy/argocd/platform/values.yaml).
"""

import argparse
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CHART = "oci://ghcr.io/easypeasyops/charts/service"
PLATFORM = "https://github.com/easypeasyops/platform.git"  # deploy/app-namespace
SIGNER = "easypeasyops/platform/.github/workflows/release-chart.yml"
# The apps that may set the service chart's rbac.clusterRead: read access, in every
# namespace, to the monitoring objects and to namespaces. Adding one is a review of what it
# does with that; its project.yaml then lists cluster_read_objects() for each namespace.
CLUSTER_READ_APPS = frozenset({"servicemonitor"})
RBAC = "rbac.authorization.k8s.io"
# The front door's probes: the apps' (a values file of the platform chart, in apps/ so that
# registering an app doesn't run the app e2e) and the tools' (the chart's own values).
APP_PROBES = "apps/front-door-probes.yaml"
TOOL_PROBES = "deploy/argocd/platform/values.yaml"
errors: list[str] = []
# Every app route's hostname: the listener it is on, and the env file that serves it.
hostnames: dict[str, tuple[str, str]] = {}


def fail(where: str, msg: str) -> None:
    errors.append(f"{where}: {msg}")
    print(f"::error::{where}: {msg}", file=sys.stderr)


def run(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def raw_url(repo: str, revision: str, path: str) -> str:
    """A file in a public GitHub repo at a revision."""
    prefix = "https://github.com/"
    if not (repo.startswith(prefix) and repo.endswith(".git")):
        raise ValueError(
            f"values.repoURL must be https://github.com/<owner>/<repo>.git, not {repo}"
        )
    return f"https://raw.githubusercontent.com/{repo[len(prefix) : -len('.git')]}/{revision}/{path}"


def cluster_read_objects(namespace: str) -> list[dict[str, str]]:
    """What rbac.clusterRead renders cluster-scoped in a namespace, as a project's
    clusterResourceWhitelist names it (charts/service/templates/rbac.yaml)."""
    name = f"{namespace}-cluster-read"
    return [{"group": RBAC, "kind": k, "name": name} for k in ("ClusterRole", "ClusterRoleBinding")]


def check_cluster_read(where: str, app: str, project: dict[str, Any], docs: list[Any]) -> None:
    """5. Only an app on the list renders cluster-wide access, and its project allows it."""
    allowed = project.get("clusterResourceWhitelist", [])
    for doc in docs:
        if doc["kind"] not in ("ClusterRole", "ClusterRoleBinding"):
            continue
        what = f"{doc['kind']} {doc['metadata']['name']}"
        if app not in CLUSTER_READ_APPS:
            fail(where, f"renders {what} (rbac.clusterRead), but {app} isn't in CLUSTER_READ_APPS")
        elif {"group": RBAC, "kind": doc["kind"], "name": doc["metadata"]["name"]} not in allowed:
            fail(where, f"project {app} doesn't allow {what}: Argo CD would refuse the sync")


def check(env_file: Path, out: Path) -> None:
    where = str(env_file.relative_to(ROOT))
    spec: dict[str, Any] = yaml.safe_load(env_file.read_text())
    try:
        app, env = spec["app"], spec["env"]
        version, digest = spec["chart"]["version"], spec["chart"]["digest"]
        repo, revision, files = (spec["values"][k] for k in ("repoURL", "revision", "files"))
    except (KeyError, TypeError) as e:
        return fail(where, f"missing field {e}")
    if (app, env) != (env_file.parent.parent.name, env_file.stem):
        return fail(where, f"app/env {app}/{env} don't match the path")

    # 2. The app's project lets this Application do what it will do.
    project_file = env_file.parent.parent / "project.yaml"
    if not project_file.exists():
        return fail(where, f"no {project_file.relative_to(ROOT)}")
    project = yaml.safe_load(project_file.read_text())["spec"]
    namespace = f"{app}-{env}"
    if namespace not in {d.get("namespace") for d in project.get("destinations", [])}:
        fail(where, f"project {app} doesn't allow namespace {namespace}")
    created = {"group": "", "kind": "Namespace", "name": namespace}
    if created not in project.get("clusterResourceWhitelist", []):
        fail(where, f"project {app} doesn't let its Application create namespace {namespace}")
    for source in (CHART, repo, PLATFORM):
        if source not in project.get("sourceRepos", []):
            fail(where, f"project {app} doesn't allow source {source}")

    # 3. The digest is a published release of the version the file names.
    try:
        published = yaml.safe_load(run("helm", "show", "chart", f"{CHART}@{digest}"))
    except subprocess.CalledProcessError as e:
        return fail(where, f"chart {digest} isn't published: {e.stderr.strip()}")
    if published.get("version") != version:
        fail(where, f"chart {digest} is version {published.get('version')}, not {version}")
    if os.environ.get("VERIFY") == "1":
        verify = subprocess.run(
            ["gh", "attestation", "verify", f"{CHART}@{digest}", "--repo", "easypeasyops/platform",
             "--signer-workflow", SIGNER],
            capture_output=True, text=True,
        )  # fmt: skip
        if verify.returncode != 0:
            fail(where, f"chart {digest}'s provenance doesn't verify: {verify.stderr.strip()}")

    # 4. It renders, with the app's values from its repo, as Argo CD will.
    with tempfile.TemporaryDirectory() as tmp:
        args: list[str] = []
        for i, path in enumerate(files):
            local = Path(tmp) / f"{i}-{Path(path).name}"
            try:
                with urllib.request.urlopen(raw_url(repo, revision, path), timeout=30) as r:  # noqa: S310
                    local.write_bytes(r.read())
            except Exception as e:
                return fail(where, f"values file {path} at {repo}@{revision}: {e}")
            args += ["--values", str(local)]
        try:
            rendered = run(
                "helm", "template", app, f"{CHART}@{digest}", "--namespace", namespace, *args,
                "--kube-version", "1.37.0",
                "--api-versions", "monitoring.coreos.com/v1/ServiceMonitor",
                "--api-versions", "monitoring.coreos.com/v1/PodMonitor",
                "--api-versions", "monitoring.coreos.com/v1/PrometheusRule",
            )  # fmt: skip
        except subprocess.CalledProcessError as e:
            return fail(where, f"doesn't render: {e.stderr.strip()}")
    (out / f"{namespace}.yaml").write_text(rendered)
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    check_cluster_read(where, app, project, docs)
    for route in (d for d in docs if d["kind"] == "HTTPRoute"):
        # No sectionName: every listener that admits the namespace, for an app's the users'.
        listener = route["spec"]["parentRefs"][0].get("sectionName", "http")
        hostnames.update(dict.fromkeys(route["spec"].get("hostnames") or [], (listener, where)))
    kinds = sorted({d["kind"] for d in docs})
    print(f"ok {where}: service {version} renders {', '.join(kinds)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="where renders are written")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    env_files = sorted(ROOT.glob("apps/*/envs/*.yaml"))
    if not env_files:
        print("::error::no apps/*/envs/*.yaml", file=sys.stderr)
        return 1
    for env_file in env_files:
        check(env_file, args.out)
    apps = yaml.safe_load((ROOT / APP_PROBES).read_text())["appFrontDoorProbes"]
    tools = yaml.safe_load((ROOT / TOOL_PROBES).read_text())["frontDoorProbes"]
    check_probes(apps, tools)
    return 1 if errors else 0


def check_probes(apps: list[dict[str, str]], tools: list[dict[str, str]]) -> None:
    """6. The front door's probes (the platform chart's Probes) and the apps' hostnames agree,
    listener included: a hostname nobody probes is a broken route nobody hears about; a probe
    of a hostname no route serves fails for good. The tools' own routes (deploy/kind/tools/)
    aren't an app's: tests/test_front_door.py holds their probes. Only when every app
    rendered: otherwise the list is short."""
    if errors:
        return
    # hostname -> (listener, the file and the list it is in)
    probed = {p["hostname"]: (p["listener"], APP_PROBES, "appFrontDoorProbes") for p in apps}
    probed |= {p["hostname"]: (p["listener"], TOOL_PROBES, "frontDoorProbes") for p in tools}
    tool_routes = {
        hostname
        for f in sorted((ROOT / "deploy/kind/tools").glob("*.yaml"))
        for route in yaml.safe_load_all(f.read_text())
        if route
        for hostname in route["spec"]["hostnames"]
    }
    for hostname, (listener, env_file) in sorted(hostnames.items()):
        # Not probed at all: a new app's hostname goes in the apps' list.
        got, where, key = probed.get(hostname, ("", APP_PROBES, "appFrontDoorProbes"))
        if got != listener:
            fail(where, f"{hostname} ({env_file}) is not in {key} on listener {listener}")
    for hostname in sorted(probed.keys() - hostnames.keys() - tool_routes):
        _, where, key = probed[hostname]
        fail(where, f"{key} has {hostname}, which no app's route serves")


if __name__ == "__main__":
    sys.exit(main())
