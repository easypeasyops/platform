"""The restore drills (PT-44) run scripts/restore-db.sh from a ConfigMap in their namespace:
it must be the script, byte for byte. Regenerate it with
`UPDATE=1 uv run pytest -n 0 tests/test_restore_drill.py`."""

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts/restore-db.sh"
CONFIGMAP = ROOT / "deploy/restore-drill/restore-db.configmap.yaml"
HEADER = """\
# Generated from scripts/restore-db.sh: don't edit. The restore drills run it from here
# (deploy/restore-drill/). Regenerate: UPDATE=1 uv run pytest -n 0 tests/test_restore_drill.py
"""


class _Literal(str):
    """A string dumped as a `|` block, so the script reads as itself in the ConfigMap."""


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(
    _Literal, lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", v, style="|")
)


def _render() -> str:
    doc = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "restore-db", "namespace": "restore-drill"},
        "data": {"restore-db.sh": _Literal(SCRIPT.read_text())},
    }
    return HEADER + yaml.dump(doc, Dumper=_Dumper, sort_keys=False, width=1000)


def test_the_drills_run_the_script_as_it_is_in_git() -> None:
    if os.environ.get("UPDATE"):
        CONFIGMAP.write_text(_render())
    configmap = yaml.safe_load(CONFIGMAP.read_text())
    assert configmap["data"]["restore-db.sh"] == SCRIPT.read_text(), (
        "stale: UPDATE=1 uv run pytest -n 0 tests/test_restore_drill.py"
    )
