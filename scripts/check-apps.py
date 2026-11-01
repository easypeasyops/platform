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
  2. apps/<app>/project.yaml exists and allows the app's namespace (<app>-<env>), its values
     repo and the service chart;
  3. the chart digest is published, and is the version the file says; with VERIFY=1, the
     platform's release workflow signed it (gh attestation verify, as the file asks);
  4. the chart at that digest renders with the app's values files, fetched from its repo at
     the named revision and layered in order, as Argo CD will render them. The renders are
     written to --out for kubeconform.
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
SIGNER = "easypeasyops/platform/.github/workflows/release-chart.yml"
errors: list[str] = []


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
    for source in (CHART, repo):
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
    kinds = sorted({d["kind"] for d in yaml.safe_load_all(rendered) if d})
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
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
