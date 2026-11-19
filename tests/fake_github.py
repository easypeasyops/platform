"""A fake GitHub for tests/test_new_app.py: stands in for `gh` and `uv` on new-app.py's PATH.

    python tests/fake_github.py gh <args>
    python tests/fake_github.py uv <args>

Everything lives in $FAKE_GH_DIR. A repo is a bare git repo, remotes/<owner>/<repo>.git, so
new-app.py's own git clones and pushes run unchanged. A release is a pushed tag, and its
notes hold the digest digest(<tag>). Merging a PR moves the repo's main to the PR's branch.
Every call is appended to calls.jsonl, the PRs are in prs.json.

uv: `uv lock` is only recorded (it would reach PyPI); `uv run --script <script>` runs the
script with this Python, except scripts/check-apps.py (helm and the network): the tests run
that one themselves.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

DIR = Path(os.environ.get("FAKE_GH_DIR", "."))


def digest(tag: str) -> str:
    """The image or chart digest the release <tag> was published with."""
    return "sha256:" + hashlib.sha256(tag.encode()).hexdigest()


def bare(repo: str) -> Path:
    return DIR / "remotes" / f"{repo}.git"


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
    ).stdout


def opt(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def releases(repo: str) -> list[dict[str, str]]:
    """Newest first, as `gh release list` lists them."""
    tags = git("tag", "--sort=-v:refname", cwd=bare(repo)).split()
    return [{"tagName": t, "body": f"Released {t}: `{digest(t)}`"} for t in tags]


def gh(args: list[str]) -> str:
    prs_file = DIR / "prs.json"
    prs: list[dict[str, Any]] = json.loads(prs_file.read_text()) if prs_file.exists() else []
    out = ""
    match args[:2]:
        case ["repo", "create"]:
            repo = args[2]
            template = bare(opt(args, "--template"))
            git("clone", "-q", "--bare", str(template), str(bare(repo)), cwd=DIR)
            git("clone", "-q", str(bare(repo)), repo.split("/")[1], cwd=Path.cwd())
        case ["pr", "create"]:
            repo = opt(args, "-R")
            url = f"https://github.com/{repo}/pull/{len(prs) + 1}"
            prs.append(
                {"url": url, "repo": repo, "head": opt(args, "--head"), "merged": False}
                | {"title": opt(args, "--title"), "body": opt(args, "--body")}
            )
            out = url + "\n"
        case ["pr", "merge"]:
            pr = next(p for p in prs if p["url"] == args[2])
            git("update-ref", "refs/heads/main", f"refs/heads/{pr['head']}", cwd=bare(pr["repo"]))
            pr["merged"] = True
        case ["pr", "view"]:
            pr = next(p for p in prs if p["url"] == args[2])
            out = "MERGED\n" if pr["merged"] else "OPEN\n"
        case ["run", "list"]:
            ran = releases(opt(args, "-R"))
            out = json.dumps([{"databaseId": 1, "status": "completed"}] if ran else [])
        case ["release", "list"]:
            out = json.dumps([{"tagName": r["tagName"]} for r in releases(opt(args, "-R"))])
        case ["release", "view"]:
            rels = releases(opt(args, "-R"))
            tag = rels[0]["tagName"] if args[2].startswith("-") else args[2]
            rel = next(r for r in rels if r["tagName"] == tag)
            out = json.dumps({k: rel[k] for k in opt(args, "--json").split(",")})
        case ["repo", "edit"] | ["api", _]:
            pass
        case _:
            sys.exit(f"fake gh: not faked: {args}")
    prs_file.write_text(json.dumps(prs))
    return out


def uv(args: list[str]) -> str:
    if args[0] == "run":
        script, *rest = (a for a in args[1:] if a not in ("--quiet", "--script"))
        if Path(script).name != "check-apps.py":
            subprocess.run([sys.executable, script, *rest], check=True)
    return ""


def main() -> None:
    tool, *args = sys.argv[1:]
    stdin = sys.stdin.read() if "--input" in args else None
    with (DIR / "calls.jsonl").open("a") as log:
        log.write(json.dumps({"cmd": [tool, *args], "cwd": os.getcwd(), "stdin": stdin}) + "\n")
    sys.stdout.write(gh(args) if tool == "gh" else uv(args))


if __name__ == "__main__":
    main()
