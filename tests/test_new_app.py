"""scripts/new-app.py end to end, against a fake GitHub (tests/fake_github.py).

The template repo, the app's repo and the platform's are bare git repos in a temporary
directory; gh and uv are fakes on PATH. new-app.py runs from a temporary platform checkout
holding the files it and check-apps.py use, so its worktrees, branches and pushes stay
there. No network, no Docker: a broken onboarding fails here, not on the next real app.

The template repo is a model of easypeasyops/app-template (TEMPLATE): the layout new-app.py's
docstring asks of a template, with python, java and rust templates.
"""

import importlib.util
import io
import itertools
import json
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.fake_github import digest

ROOT = Path(__file__).resolve().parent.parent
FAKE = Path(__file__).resolve().parent / "fake_github.py"
ORG = "easypeasyops"
TEMPLATE = f"{ORG}/app-template"
LANGS = ("python", "java", "rust")
# What new-app.py and check-apps.py read from the platform checkout they run in.
PLATFORM_FILES = [
    "scripts/new-app.py",
    "scripts/check-apps.py",
    "scripts/gen_slo_rules.py",
    "deploy/argocd/platform/values.yaml",
    *(str(p.relative_to(ROOT)) for p in sorted((ROOT / "deploy/kind/tools").glob("*.yaml"))),
]
CHART_TAGS = ("service/v1.7.0", "service/v1.7.1")  # the platform's chart releases
# Git without the developer's config (signing, hooks, default branch).
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}
SLOS = """window: 30d
runbook_url: https://github.com/{repo}/blob/main/README.md#slo-burn-rate-alerts
slos:
  - name: {app}-availability
    description: The app answers without a server error
    objective: 0.995
    sli:
      type: availability
      selector: 'job="{app}", route!~"/healthz|/readyz|unmatched"'
"""
ENV = """environment: {env}
image:
  digest: "" # no release deployed yet
gateway:
  hostnames: ["{host}"]
"""
# Each language's build files, which name the template only as text.
BUILD = {
    "python": {"pyproject.toml": 'name = "{app}"\n', "uv.lock": 'name = "{app}"\n'},
    "java": {"pom.xml": "<artifactId>{app}</artifactId>\n"},
    "rust": {"Cargo.toml": 'name = "{app}"\n', "Cargo.lock": 'name = "{app}"\n'},
}


# app-template's CI and Dependabot, trimmed: each template's jobs and entries name its folder.
CI = """name: CI
on: [pull_request]
jobs:
  scope:
    runs-on: ubuntu-latest
    steps:
      - run: scripts/ci-scope.sh
  check:
    needs: scope
    uses: ./.github/workflows/check.yml
    with:
      working-directory: apps/python-template
  image:
    needs: scope
    uses: ./.github/workflows/image.yml
    with:
      context: apps/python-template
  # The java template's.
  java-check:
    needs: scope
    uses: ./.github/workflows/check.yml
    with:
      working-directory: apps/java-template
  rust-check:
    needs: scope
    uses: ./.github/workflows/check.yml
    with:
      working-directory: apps/rust-template
  rust-image:
    needs: scope
    uses: ./.github/workflows/image.yml
    with:
      context: apps/rust-template
  deploy:
    needs: scope
    runs-on: ubuntu-latest
    steps:
      - run: ls apps
  gate:
    name: ci
    needs: [scope, check, image, java-check, rust-check, rust-image, deploy]
    runs-on: ubuntu-latest
    steps:
      - run: "true"
"""
DEPENDABOT = """version: 2
updates:
  - package-ecosystem: uv
    directories: [/apps/python-template]
  # Spring Boot's parent pins most versions.
  - package-ecosystem: maven
    directories: [/apps/java-template]
  - package-ecosystem: cargo
    directories: [/apps/rust-template]
  - package-ecosystem: docker
    directories: [/apps/*]
"""


def _template(platform_sha: str) -> dict[str, str]:
    """The template repo's files."""
    files = {
        "platform.lock": f"{ORG}/platform@{platform_sha}\n",
        "README.md": f"# {TEMPLATE}\n",
        ".github/workflows/ci.yml": CI,
        ".github/dependabot.yaml": DEPENDABOT,
    }
    for lang in LANGS:
        app = f"{lang}-template"
        d = f"apps/{app}"
        files |= {
            f"{d}/deploy/service/values.yaml": f"image:\n  repository: ghcr.io/{TEMPLATE}/{app}\n",
            f"{d}/deploy/service/slo-rules.values.yaml": "# stale: create regenerates it\n",
            f"{d}/deploy/prometheus/slos.yaml": SLOS.format(repo=TEMPLATE, app=app),
            f"{d}/deploy/envs/staging.yaml": ENV.format(
                env="staging", host=f"staging.{app}.localhost"
            ),
            f"{d}/deploy/envs/prod.yaml": ENV.format(env="prod", host=f"{app}.localhost"),
        }
        files |= {f"{d}/{name}": text.format(app=app) for name, text in BUILD[lang].items()}
    return files


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
        env=os.environ | GIT_ENV,
    ).stdout


def _commit(work: Path, files: dict[str, str], bare: Path) -> str:
    """A repo of files at work, pushed to a new bare repo; its commit."""
    for name, text in files.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(text)
    _git("init", "-q", "-b", "main", cwd=work)
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "init", cwd=work)
    _git("init", "-q", "--bare", "-b", "main", str(bare), cwd=work)
    _git("remote", "add", "origin", str(bare), cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)
    return _git("rev-parse", "HEAD", cwd=work).strip()


@dataclass
class World:
    """A fake GitHub, a platform checkout, and the folder new-app.py runs from."""

    root: Path
    env: dict[str, str]

    @property
    def platform(self) -> Path:
        return self.root / "platform"

    @property
    def work(self) -> Path:
        return self.root / "work"

    def bare(self, repo: str) -> Path:
        return self.root / "gh/remotes" / f"{repo}.git"

    def new_app(self, *args: str) -> subprocess.CompletedProcess[str]:
        script = self.platform / "scripts/new-app.py"
        result = subprocess.run(
            [sys.executable, str(script), *args],
            cwd=self.work,
            env=self.env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return result

    def show(self, repo: str, ref: str, path: str) -> str:
        """A file in a repo of the fake GitHub, at a ref."""
        return _git("show", f"{ref}:{path}", cwd=self.bare(repo))

    def files(self, repo: str, ref: str = "main") -> list[str]:
        return _git("ls-tree", "-r", "--name-only", ref, cwd=self.bare(repo)).split()

    def calls(self, *prefix: str) -> list[dict[str, Any]]:
        """The fake gh and uv calls whose command starts with prefix."""
        lines = (self.root / "gh/calls.jsonl").read_text().splitlines()
        calls = [json.loads(line) for line in lines]
        return [c for c in calls if c["cmd"][: len(prefix)] == list(prefix)]


@pytest.fixture
def world(tmp_path: Path) -> World:
    """The platform's repo (PLATFORM_FILES, no apps yet, and CHART_TAGS) and the template's."""
    gh = tmp_path / "gh"
    bin_dir = tmp_path / "bin"
    for d in (gh, bin_dir, tmp_path / "work"):
        d.mkdir()
    for tool in ("gh", "uv"):
        (bin_dir / tool).write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" {tool} "$@"\n')
        (bin_dir / tool).chmod(0o755)
    env = os.environ | GIT_ENV | {"FAKE_GH_DIR": str(gh), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    w = World(tmp_path, env)

    files = {f: (ROOT / f).read_text() for f in PLATFORM_FILES}
    # The real file's header, and no app's probes yet: this platform runs no app.
    probes = (ROOT / "apps/front-door-probes.yaml").read_text()
    files["apps/front-door-probes.yaml"] = re.sub(
        r"(?ms)^appFrontDoorProbes:\n.*", "appFrontDoorProbes: []\n", probes
    )
    # servicemonitor's probe is with the tools' (it is on their listener): it goes too.
    tools = files["deploy/argocd/platform/values.yaml"]
    files["deploy/argocd/platform/values.yaml"] = re.sub(r".*# servicemonitor-.*\n", "", tools)
    sha = _commit(w.platform, files, w.bare(f"{ORG}/platform"))
    for tag in CHART_TAGS:
        _git("tag", tag, cwd=w.platform)
    _git("push", "-q", "origin", "--tags", cwd=w.platform)
    _git("fetch", "-q", "origin", cwd=w.platform)

    _commit(tmp_path / "template", _template(sha), w.bare(TEMPLATE))
    return w


def test_create_keeps_one_template_and_renames_it(world: World) -> None:
    world.new_app("create", "shop", "--lang", "python")
    repo = f"{ORG}/shop"
    files = world.files(repo)
    assert {f.split("/")[1] for f in files if f.startswith("apps/")} == {"shop"}
    assert "apps/shop/deploy/envs/staging.yaml" in files
    # No name of the template repo or of any template is left, in any file.
    for name in ("app-template", *(f"{lang}-template" for lang in LANGS)):
        grep = subprocess.run(
            ["git", "grep", "-l", "-e", name, "main"], cwd=world.bare(repo), capture_output=True
        )
        assert grep.returncode == 1, f"{name} left in {grep.stdout!r}"
    values = yaml.safe_load(world.show(repo, "main", "apps/shop/deploy/service/values.yaml"))
    assert values["image"]["repository"] == f"ghcr.io/{repo}/shop"
    staging = yaml.safe_load(world.show(repo, "main", "apps/shop/deploy/envs/staging.yaml"))
    assert staging["gateway"]["hostnames"] == ["staging.shop.localhost"]


def test_create_drops_the_other_templates_ci_jobs_and_dependabot_entries(world: World) -> None:
    world.new_app("create", "shop", "--lang", "python")
    ci = yaml.safe_load(world.show(f"{ORG}/shop", "main", ".github/workflows/ci.yml"))
    assert list(ci["jobs"]) == ["scope", "check", "image", "deploy", "gate"]
    assert ci["jobs"]["gate"]["needs"] == ["scope", "check", "image", "deploy"]
    bot = yaml.safe_load(world.show(f"{ORG}/shop", "main", ".github/dependabot.yaml"))
    assert [(u["package-ecosystem"], u["directories"]) for u in bot["updates"]] == [
        ("uv", ["/apps/shop"]),
        ("docker", ["/apps/*"]),
    ]


def test_create_regenerates_the_slo_rules_for_the_app(world: World) -> None:
    world.new_app("create", "shop", "--lang", "python")
    rules = world.show(f"{ORG}/shop", "main", "apps/shop/deploy/service/slo-rules.values.yaml")
    assert "name: slo-shop-availability" in rules
    # What the generator at platform.lock's commit writes: its own --check agrees.
    subprocess.run(
        [sys.executable, str(world.platform / "scripts/gen_slo_rules.py"), "--check",
         "--spec", "deploy/prometheus/slos.yaml",
         "--values", "--out", "deploy/service/slo-rules.values.yaml"],
        cwd=world.work / "shop/apps/shop", check=True, capture_output=True,
    )  # fmt: skip
    lock = world.calls("uv", "lock")
    assert [Path(c["cwd"]).name for c in lock] == ["shop"]


def test_create_sets_up_the_repo_and_tags_the_first_release(world: World) -> None:
    world.new_app("create", "shop", "--lang", "python")
    repo = f"{ORG}/shop"
    [create] = world.calls("gh", "repo", "create")
    assert create["cmd"][4:] == ["--public", "--template", TEMPLATE, "--clone"]
    [edit] = world.calls("gh", "repo", "edit", repo)
    assert {"--enable-squash-merge", "--enable-auto-merge", "--delete-branch-on-merge"} <= set(
        edit["cmd"]
    )
    [ruleset] = world.calls("gh", "api", "-X", "POST", f"repos/{repo}/rulesets")
    rules = json.loads(ruleset["stdin"])["rules"]
    assert rules[0]["parameters"]["required_status_checks"] == [{"context": "ci"}]
    assert _git("tag", cwd=world.bare(repo)).split() == ["shop/v0.1.0"]


def _check_apps(world: World, branch: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """scripts/check-apps.py, as the platform's branch has it, on that branch, with helm and
    GitHub's raw files faked: the chart releases are CHART_TAGS, and render the HTTPRoute of
    the layered values' gateway.hostnames; an app's values files come from its fake repo."""
    checkout = world.root / f"checkout-{branch}"
    platform = world.bare(f"{ORG}/platform")
    _git("clone", "-q", "-b", branch, str(platform), str(checkout), cwd=world.root)
    spec = importlib.util.spec_from_file_location("check_apps", checkout / "scripts/check-apps.py")
    assert spec
    assert spec.loader
    check_apps = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check_apps)
    charts = {digest(t): t.removeprefix("service/v") for t in CHART_TAGS}

    def helm(*cmd: str) -> str:
        chart = next(a for a in cmd if a.startswith("oci://")).split("@")[1]
        if chart not in charts:
            raise subprocess.CalledProcessError(1, cmd, stderr="not found")
        if cmd[1] == "show":
            return yaml.safe_dump({"version": charts[chart]})
        values: dict[str, Any] = {}
        for flag, path in itertools.pairwise(cmd):
            if flag == "--values":
                values |= yaml.safe_load(Path(path).read_text())
        route = {
            "parentRefs": [{"name": "front-door"}],
            "hostnames": values["gateway"]["hostnames"],
        }
        return yaml.safe_dump({"kind": "HTTPRoute", "metadata": {"name": cmd[2]}, "spec": route})

    def urlopen(url: str, timeout: float) -> io.BytesIO:
        raw = url.removeprefix("https://raw.githubusercontent.com/")
        owner, repo, revision, path = raw.split("/", 3)
        return io.BytesIO(world.show(f"{owner}/{repo}", revision, path).encode())

    monkeypatch.setattr(check_apps, "run", helm)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sys, "argv", ["check-apps.py", "--out", str(world.root / "renders")])
    assert check_apps.main() == 0, check_apps.errors


def _platform_pr(world: World, env: str) -> dict[str, Any]:
    """The platform PR that adds the app's env."""
    prs = json.loads((world.root / "gh/prs.json").read_text())
    [pr] = [p for p in prs if p["repo"] == f"{ORG}/platform" and p["head"] == f"apps-shop-{env}"]
    return dict(pr)


def test_staging_deploys_the_release_and_registers_the_app(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.new_app("create", "shop", "--lang", "python")
    world.new_app("staging", "shop")
    # The app's staging runs its first release: the deploy PR merged.
    app_env = world.show(f"{ORG}/shop", "main", "apps/shop/deploy/envs/staging.yaml")
    assert yaml.safe_load(app_env)["image"]["digest"] == digest("shop/v0.1.0")

    branch = "apps-shop-staging"
    env = yaml.safe_load(world.show(f"{ORG}/platform", branch, "apps/shop/envs/staging.yaml"))
    assert env == {
        "app": "shop",
        "env": "staging",
        "chart": {"version": "1.7.1", "digest": digest("service/v1.7.1")},  # the latest
        "values": {
            "repoURL": f"https://github.com/{ORG}/shop.git",
            "revision": "main",
            "files": ["apps/shop/deploy/service/values.yaml", "apps/shop/deploy/envs/staging.yaml"],
        },
    }
    project = yaml.safe_load(world.show(f"{ORG}/platform", branch, "apps/shop/project.yaml"))
    assert [d["namespace"] for d in project["spec"]["destinations"]] == ["shop-staging"]
    probes = yaml.safe_load(world.show(f"{ORG}/platform", branch, "apps/front-door-probes.yaml"))
    assert probes["appFrontDoorProbes"] == [
        {"hostname": "staging.shop.localhost", "listener": "http", "path": "/healthz"}
    ]
    assert _platform_pr(world, "staging")["merged"]  # auto-merge
    _check_apps(world, branch, monkeypatch)


def test_prod_promotes_staging_and_leaves_the_merge_to_emrah(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.new_app("create", "shop", "--lang", "python")
    world.new_app("staging", "shop")
    world.new_app("prod", "shop")
    app_env = world.show(f"{ORG}/shop", "main", "apps/shop/deploy/envs/prod.yaml")
    assert yaml.safe_load(app_env)["image"]["digest"] == digest("shop/v0.1.0")

    branch = "apps-shop-prod"
    env = yaml.safe_load(world.show(f"{ORG}/platform", branch, "apps/shop/envs/prod.yaml"))
    assert env["chart"] == {"version": "1.7.1", "digest": digest("service/v1.7.1")}  # staging's
    assert env["values"]["files"] == [
        "apps/shop/deploy/service/values.yaml",
        "apps/shop/deploy/service/slo-rules.values.yaml",  # prod ships the SLO rules
        "apps/shop/deploy/envs/prod.yaml",
    ]
    project = yaml.safe_load(world.show(f"{ORG}/platform", branch, "apps/shop/project.yaml"))
    namespaces = [d["namespace"] for d in project["spec"]["destinations"]]
    assert namespaces == ["shop-staging", "shop-prod"]
    probes = yaml.safe_load(world.show(f"{ORG}/platform", branch, "apps/front-door-probes.yaml"))
    hostnames = [p["hostname"] for p in probes["appFrontDoorProbes"]]
    assert hostnames == ["staging.shop.localhost", "shop.localhost"]
    pr = _platform_pr(world, "prod")
    assert not pr["merged"]
    assert "Emrah merges it" in pr["body"]
    assert not world.calls("gh", "pr", "merge", pr["url"])
    _check_apps(world, branch, monkeypatch)
