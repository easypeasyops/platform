"""A fake GitHub for tests/test_new_app.py: stands in for `gh` and `uv` on new-app.py's PATH.

    python tests/fake_github.py gh <args>
    python tests/fake_github.py uv <args>

Everything lives in $FAKE_GH_DIR. A repo is a bare git repo, remotes/<owner>/<repo>.git, so
new-app.py's own git clones and pushes run unchanged. Every call is appended to calls.jsonl.

uv: `uv lock` is only recorded (it would reach PyPI); `uv run --script <script>` runs the
script with this Python, except scripts/check-apps.py (helm and the network): the tests run
that one themselves.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

DIR = Path(os.environ.get("FAKE_GH_DIR", "."))


def bare(repo: str) -> Path:
    return DIR / "remotes" / f"{repo}.git"


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
    ).stdout


def opt(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def gh(args: list[str]) -> str:
    out = ""
    match args[:2]:
        case ["repo", "create"]:
            repo = args[2]
            template = bare(opt(args, "--template"))
            git("clone", "-q", "--bare", str(template), str(bare(repo)), cwd=DIR)
            git("clone", "-q", str(bare(repo)), repo.split("/")[1], cwd=Path.cwd())
        case ["repo", "edit"] | ["api", _]:
            pass
        case _:
            sys.exit(f"fake gh: not faked: {args}")
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
