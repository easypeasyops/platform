#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml==6.0.3"]
# ///
"""Take a new app from a template repo to prod, in three steps that each stop at a wait.

    uv run --script <platform>/scripts/new-app.py create  <app> --lang python   # or java, rust, ...
    uv run --script <platform>/scripts/new-app.py staging <app>
    uv run --script <platform>/scripts/new-app.py prod    <app>

Run it from the folder that holds your checkouts: the app's repo, easypeasyops/<app>
(--repo names it otherwise; pass it to every step), is cloned to ./<repo> (--dir changes
that). The platform side is done in a throwaway worktree of this checkout, cut from
origin/main, so the checkout you run it from is left alone.

  create   creates the app's repo from the template repo and clones it. It keeps the one
           template --lang names, apps/<lang>-template: the others' folders go, and so
           do the CI jobs and Dependabot entries that name them. Then it renames
           <lang>-template to <app> and the template repo to <repo> (its URLs, the image's
           ghcr.io/easypeasyops/<repo>/<app>), refreshes the lockfile, regenerates the SLO
           rules with gen_slo_rules.py at the app's platform.lock commit, and pushes. It
           sets the repo to squash-only with auto-merge, makes `ci` required on main, and
           tags <app>/v<version>, which releases the image. You add the repo to the
           Renovate app by hand.
  staging  waits for the release and puts its image digest in the app's
           deploy/envs/staging.yaml (a PR that auto-merges). Then it opens the platform PR
           that adds apps/<app>/ with staging: the project, envs/staging.yaml at the latest
           service chart release, and the front-door probe. That PR auto-merges, and Argo CD
           deploys staging.
  prod     promotes the digest staging runs to deploy/envs/prod.yaml (a PR that
           auto-merges). Then it opens the platform PR that adds prod, at the chart version
           staging pins. That PR changes apps/<app>/envs/prod.yaml, so it doesn't
           auto-merge: Emrah merges it.

What a template repo must have (app-template does; a new language's template, such as
go-template, joins it as apps/go-template/ and needs the same):
- apps/<lang>-template/ per template. Its name is its only placeholder.
- In .github/workflows/ci.yml, each template's jobs name its folder (`apps/<lang>-template`)
  and no other's, and the `ci` gate lists them in a one-line `needs: [...]`. The same in
  .github/dependabot.yaml, for its entries.
- Each template's deploy/ layout: service/values.yaml, service/slo-rules.values.yaml,
  prometheus/slos.yaml, and envs/staging.yaml and envs/prod.yaml, each with one
  `digest:` line (image.digest) and gateway.hostnames.
- A platform.lock.
- A release.yml that releases an <app>/vX.Y.Z tag and puts the image digest in its notes.
The lockfile refresh is the only step that depends on the language (LOCKFILES).
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

ORG = "easypeasyops"
PLATFORM = Path(__file__).resolve().parent.parent
PROBES = PLATFORM / "deploy/argocd/platform/values.yaml"
# A lockfile in apps/<app>/ -> the command that refreshes it after the rename. Others, such
# as Cargo.lock, name the app only as text, and the rename already changed it.
LOCKFILES = {"uv.lock": ["uv", "lock"]}
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def run(*cmd: str, cwd: Path | None = None) -> str:
    print("+", " ".join(cmd), file=sys.stderr)
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE).stdout


def wait_merged(pr: str, repo: str) -> None:
    print(f"waiting for {pr} to merge", file=sys.stderr)
    view = ["gh", "pr", "view", pr, "-R", repo, "--json", "state", "-q", ".state"]
    for _ in range(90):  # 30 minutes; quietly, not a `run` line every 20 seconds
        if (
            subprocess.run(view, check=True, text=True, capture_output=True).stdout.strip()
            == "MERGED"
        ):
            return
        time.sleep(20)
    sys.exit(f"{pr} not merged after 30 minutes")


def open_pr(repo: str, cwd: Path, branch: str, title: str, body: str, auto: bool) -> str:
    run("git", "push", "-u", "origin", branch, cwd=cwd)
    url = run(
        "gh",
        "pr",
        "create",
        "-R",
        repo,
        "--head",
        branch,
        "--title",
        title,
        "--body",
        body,
        cwd=cwd,
    ).strip()
    if auto:
        run("gh", "pr", "merge", url, "-R", repo, "--auto", "--squash", "--delete-branch")
    print(url)
    return url


def set_app_digest(app: str, repo: str, d: Path, env: str, digest: str, version: str) -> None:
    """Set image.digest in the app's deploy/envs/<env>.yaml with a PR; wait for its merge."""
    run("git", "fetch", "origin", cwd=d)
    branch = f"deploy-{env}-{version}"
    run("git", "switch", "-c", branch, "origin/main", cwd=d)
    f = d / f"apps/{app}/deploy/envs/{env}.yaml"
    text, n = re.subn(r"^(\s*digest:).*$", rf"\1 {digest}", f.read_text(), flags=re.M)
    if n != 1:
        sys.exit(f"{f}: expected one `digest:` line, found {n}")
    f.write_text(text)
    run("git", "commit", "-am", f"Deploy {app} {version} to {env}", cwd=d)
    pr = open_pr(
        repo,
        d,
        branch,
        f"Deploy {app} {version} to {env}",
        f"Sets `image.digest` in `apps/{app}/deploy/envs/{env}.yaml` to {app} "
        f"{version}'s image, `{digest}`.",
        auto=True,
    )
    wait_merged(pr, repo)
    run("git", "switch", "main", cwd=d)
    run("git", "pull", "--ff-only", cwd=d)


def project_yaml(app: str, repo: str, envs: list[str]) -> str:
    dests = "".join(
        f"    - {{ server: https://kubernetes.default.svc, namespace: {app}-{e} }}\n" for e in envs
    )
    nss = "".join(f'    - {{ group: "", kind: Namespace, name: {app}-{e} }}\n' for e in envs)
    return f"""# {app}'s Argo CD project. Least privilege, as every app's: the service chart, its
# repo, the platform's repo, and its own namespaces.

apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: {app}
  namespace: argocd
spec:
  description: {app}'s environments (apps/{app}/envs)
  sourceRepos:
    - oci://ghcr.io/{ORG}/charts/service
    - https://github.com/{repo}.git
    - https://github.com/{ORG}/platform.git
  destinations:
{dests}  clusterResourceWhitelist: # by name: its own namespaces, no other
{nss}  namespaceResourceWhitelist:
    - {{ group: "*", kind: "*" }}
"""


def env_yaml(app: str, repo: str, env: str, version: str, digest: str) -> str:
    slo = f"    - apps/{app}/deploy/service/slo-rules.values.yaml\n" if env == "prod" else ""
    return f"""# {app} in {env}.
app: {app}
env: {env}
chart:
  version: {version}
  digest: {digest}
values:
  repoURL: https://github.com/{repo}.git
  revision: main
  files:
    - apps/{app}/deploy/service/values.yaml
{slo}    - apps/{app}/deploy/envs/{env}.yaml
"""


def add_probes(values: Path, app: str, env: str, hostnames: list[str]) -> None:
    """Add the env's hostnames to frontDoorProbes, after the last app (http listener) entry."""
    lines = values.read_text().splitlines(keepends=True)
    last = max(i for i, line in enumerate(lines) if "listener: http," in line)
    new = [
        f"  - {{ hostname: {h}, listener: http, path: /healthz }} # {app}-{env}\n"
        for h in hostnames
    ]
    values.write_text("".join(lines[: last + 1] + new + lines[last + 1 :]))


def platform_pr(app: str, repo: str, d: Path, env: str, chart: tuple[str, str]) -> None:
    """The platform PR that adds <env> to apps/<app>/; auto-merges unless it is prod."""
    hosts = yaml.safe_load((d / f"apps/{app}/deploy/envs/{env}.yaml").read_text())["gateway"][
        "hostnames"
    ]
    run("git", "fetch", "origin", "main", cwd=PLATFORM)
    branch = f"apps-{app}-{env}"
    with tempfile.TemporaryDirectory() as tmp:
        wt = Path(tmp) / "platform"
        run("git", "worktree", "add", "-b", branch, str(wt), "origin/main", cwd=PLATFORM)
        try:
            envs_dir = wt / f"apps/{app}/envs"
            envs_dir.mkdir(parents=True, exist_ok=True)
            (envs_dir / f"{env}.yaml").write_text(env_yaml(app, repo, env, *chart))
            envs = sorted(p.stem for p in envs_dir.glob("*.yaml"))[::-1]  # staging, prod
            (wt / f"apps/{app}/project.yaml").write_text(project_yaml(app, repo, envs))
            add_probes(wt / PROBES.relative_to(PLATFORM), app, env, hosts)
            run(
                "uv",
                "run",
                "--quiet",
                "--script",
                "scripts/check-apps.py",
                "--out",
                str(Path(tmp) / "renders"),
                cwd=wt,
            )
            title = f"apps: {app} in {env}"
            run("git", "add", "-A", cwd=wt)
            run("git", "commit", "-m", title, cwd=wt)
            body = (
                f"Adds {app}'s {env} environment: `apps/{app}/envs/{env}.yaml` (service chart "
                f"{chart[0]}), the `{app}-{env}` namespace in its project, and a front-door "
                f"probe for {', '.join(hosts)}.\n\nOn the cluster: Argo CD creates the "
                f"`{app}-{env}` namespace and deploys {app} there; the blackbox exporter "
                "starts probing it."
            )
            if env == "prod":
                body += "\n\nChanges an `envs/prod.yaml`: no auto-merge, Emrah merges it."
            open_pr(f"{ORG}/platform", wt, branch, title, body, auto=env != "prod")
        finally:
            run("git", "worktree", "remove", "--force", str(wt), cwd=PLATFORM)
            run("git", "branch", "-D", branch, cwd=PLATFORM)


def drop_blocks(path: Path, header: str, names: list[str]) -> set[str]:
    """Drop each block in a YAML file that mentions apps/<name> for any of names; return their keys.

    A block is a line matching header, the lines indented deeper under it, and the comment
    lines right above it. Text, not YAML, so the comments everywhere else stay.
    """
    lines = path.read_text().splitlines(keepends=True)
    out, dropped, i = [], set(), 0
    while i < len(lines):
        if not re.match(header, lines[i]):
            out.append(lines[i])
            i += 1
            continue
        indent = len(lines[i]) - len(lines[i].lstrip())
        end = i + 1
        while end < len(lines) and (
            not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > indent
        ):
            end += 1
        if any(f"apps/{n}" in "".join(lines[i:end]) for n in names):
            dropped.add(lines[i].strip().rstrip(":"))
            while out and out[-1].strip().startswith("#"):  # its comment
                out.pop()
        else:
            out.extend(lines[i:end])
        i = end
    path.write_text("".join(out))
    return dropped


def keep_one(d: Path, kept: str) -> None:
    """Delete every template but apps/<kept>/, with its CI jobs and Dependabot entries."""
    others = sorted(p.name for p in (d / "apps").iterdir() if p.is_dir() and p.name != kept)
    for o in others:
        run("git", "rm", "-rq", f"apps/{o}", cwd=d)
    ci = d / ".github/workflows/ci.yml"
    jobs = drop_blocks(ci, r"  [a-z0-9-]+:$", others)
    ci.write_text(
        re.sub(
            r"^(\s*needs: \[)(.*)\]$",
            lambda m: m[1] + ", ".join(n for n in m[2].split(", ") if n not in jobs) + "]",
            ci.read_text(),
            flags=re.M,
        )
    )
    drop_blocks(d / ".github/dependabot.yaml", r"  - package-ecosystem:", others)


def create(app: str, repo: str, d: Path, template: str, lang: str, version: str) -> None:
    placeholder = f"{lang}-template"
    run("gh", "repo", "create", repo, "--public", "--template", template, "--clone", cwd=d.parent)
    name = repo.split("/")[1]
    if d.name != name:
        (d.parent / name).rename(d)
    if not (d / f"apps/{placeholder}").is_dir():
        have = ", ".join(sorted(p.name for p in (d / "apps").iterdir()))
        sys.exit(f"{template} has no apps/{placeholder}/, only: {have}")
    keep_one(d, placeholder)
    run("git", "mv", f"apps/{placeholder}", f"apps/{app}", cwd=d)
    for f in run("git", "grep", "-l", "-e", placeholder, "-e", template, cwd=d).split():
        p = d / f
        # The template's repo first (its URLs, the image's <repo> part), then the app.
        p.write_text(p.read_text().replace(template, repo).replace(placeholder, app))
    appdir = d / f"apps/{app}"
    for lockfile, cmd in LOCKFILES.items():
        if (appdir / lockfile).exists():
            run(*cmd, cwd=appdir)
    lock = (d / "platform.lock").read_text().split("@")[-1].strip()
    run("git", "fetch", "origin", lock, cwd=PLATFORM)
    with tempfile.TemporaryDirectory() as tmp:
        gen = Path(tmp) / "gen_slo_rules.py"
        gen.write_text(run("git", "show", f"{lock}:scripts/gen_slo_rules.py", cwd=PLATFORM))
        run(
            "uv",
            "run",
            "--script",
            str(gen),
            "--spec",
            "deploy/prometheus/slos.yaml",
            "--values",
            "--out",
            "deploy/service/slo-rules.values.yaml",
            cwd=appdir,
        )
    run("git", "add", "-A", cwd=d)
    run("git", "commit", "-m", f"Start {app} from {template}", cwd=d)
    run("git", "push", "origin", "main", cwd=d)
    run(
        "gh",
        "repo",
        "edit",
        repo,
        "--enable-squash-merge",
        "--enable-merge-commit=false",
        "--enable-rebase-merge=false",
        "--enable-auto-merge",
        "--delete-branch-on-merge",
    )
    ruleset: dict[str, Any] = {
        "name": "main",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    "required_status_checks": [{"context": "ci"}],
                },
            }
        ],
    }
    subprocess.run(
        ["gh", "api", "-X", "POST", f"repos/{repo}/rulesets", "--input", "-"],
        input=json.dumps(ruleset),
        text=True,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    tag = f"{app}/v{version}"
    run("git", "tag", tag, cwd=d)
    run("git", "push", "origin", tag, cwd=d)
    same_repo = "" if repo == f"{ORG}/{app}" else f" --repo {repo.split('/')[1]}"
    print(
        f"\nTagged {tag}: release.yml is releasing the image. By hand: add {repo} to the "
        f"Renovate app. Next: new-app.py staging {app}{same_repo}"
    )


def staging(app: str, repo: str, d: Path) -> None:
    runs = json.loads(
        run(
            "gh",
            "run",
            "list",
            "-R",
            repo,
            "--workflow",
            "release.yml",
            "-L",
            "1",
            "--json",
            "databaseId,status",
        )
    )
    if not runs:
        sys.exit(f"no release.yml run in {repo}: run `new-app.py create` first")
    if runs[0]["status"] != "completed":
        run("gh", "run", "watch", str(runs[0]["databaseId"]), "-R", repo, "--exit-status")
    rel = json.loads(run("gh", "release", "view", "-R", repo, "--json", "tagName,body"))
    digest = DIGEST.search(rel["body"])
    if not digest:
        sys.exit(f"{rel['tagName']}'s notes have no image digest")
    set_app_digest(app, repo, d, "staging", digest[0], rel["tagName"].split("/v")[-1])
    tags = json.loads(
        run("gh", "release", "list", "-R", f"{ORG}/platform", "-L", "50", "--json", "tagName")
    )
    chart_tag = next(t["tagName"] for t in tags if t["tagName"].startswith("service/v"))
    body = json.loads(
        run("gh", "release", "view", chart_tag, "-R", f"{ORG}/platform", "--json", "body")
    )["body"]
    chart_digest = DIGEST.search(body)
    if not chart_digest:
        sys.exit(f"{chart_tag}'s notes have no chart digest")
    platform_pr(app, repo, d, "staging", (chart_tag.removeprefix("service/v"), chart_digest[0]))


def prod(app: str, repo: str, d: Path) -> None:
    run("git", "pull", "--ff-only", cwd=d)
    digest = yaml.safe_load((d / f"apps/{app}/deploy/envs/staging.yaml").read_text())["image"][
        "digest"
    ]
    if not digest:
        sys.exit("staging runs no release yet: run `new-app.py staging` first")
    # The release staging runs: the newest one whose notes hold staging's digest.
    tags = json.loads(run("gh", "release", "list", "-R", repo, "--json", "tagName"))
    version = next(
        t["tagName"].split("/v")[-1]
        for t in tags
        if digest
        in json.loads(run("gh", "release", "view", t["tagName"], "-R", repo, "--json", "body"))[
            "body"
        ]
    )
    set_app_digest(app, repo, d, "prod", digest, version)
    run("git", "fetch", "origin", "main", cwd=PLATFORM)
    staging_env = run("git", "show", f"origin/main:apps/{app}/envs/staging.yaml", cwd=PLATFORM)
    chart = yaml.safe_load(staging_env)["chart"]
    platform_pr(app, repo, d, "prod", (chart["version"], chart["digest"]))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("step", choices=["create", "staging", "prod"])
    p.add_argument("app", help="lowercase letters, digits and dashes")
    p.add_argument("--repo", help="the app's repo in easypeasyops (default: <app>)")
    p.add_argument("--dir", type=Path, help="the app's checkout (default: ./<repo>)")
    p.add_argument("--template", default=f"{ORG}/app-template", help="create: the template repo")
    p.add_argument("--lang", help="create: the template to keep, apps/<lang>-template")
    p.add_argument("--version", default="0.1.0", help="create: the first release")
    a = p.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", a.app):
        sys.exit(f"{a.app}: an app name is lowercase letters, digits and dashes")
    repo = f"{ORG}/{a.repo or a.app}"
    d = (a.dir or Path.cwd() / repo.split("/")[1]).resolve()
    if a.step == "create":
        if not a.lang:
            sys.exit("create needs --lang: which apps/<lang>-template to start from")
        create(a.app, repo, d, a.template, a.lang, a.version)
    elif a.step == "staging":
        staging(a.app, repo, d)
    else:
        prod(a.app, repo, d)


if __name__ == "__main__":
    main()
