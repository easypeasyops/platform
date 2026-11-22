"""The restore drills (PT-44) run scripts/restore-db.sh from a ConfigMap in their namespace:
it must be the script, byte for byte. Regenerate it with
`UPDATE=1 uv run pytest -n 0 tests/test_restore_drill.py`."""

import os
from pathlib import Path
from typing import Any

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


def _drill_cronjob() -> dict[str, Any]:
    policy = yaml.safe_load((ROOT / "deploy/policies/restore-drill.yaml").read_text())
    (generate,) = policy["spec"]["generate"]
    # The template's CEL placeholders are plain text to YAML: the document parses as it is.
    cronjob: dict[str, Any] = yaml.safe_load(generate["template"]["value"])
    return cronjob


def test_each_database_with_backups_gets_a_drill_that_runs_the_script() -> None:
    """One CronJob per ScheduledBackup (what the chart renders only with backups on), kept in
    step with it and deleted with it, running the script in --drill mode as the drill's
    ServiceAccount."""
    policy = yaml.safe_load((ROOT / "deploy/policies/restore-drill.yaml").read_text())
    spec = policy["spec"]
    assert spec["matchConstraints"]["resourceRules"][0]["resources"] == ["scheduledbackups"]
    assert spec["evaluation"]["synchronize"] == {"enabled": True}
    assert spec["evaluation"]["generateExisting"] == {"enabled": True}
    cronjob = _drill_cronjob()
    assert cronjob["metadata"]["namespace"] == "restore-drill"
    pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "restore-drill"
    (container,) = pod["containers"]
    assert container["command"][:3] == ["bash", "/drill/restore-db.sh", "--drill"]
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["script"]["configMap"]["name"] == "restore-db"


def test_the_drill_runs_the_service_charts_database_image() -> None:
    """The drill's container is the Postgres image every chart database already pulls (bash
    and coreutils, on the node): when the chart's default moves, the drill moves with it."""
    values = yaml.safe_load((ROOT / "charts/service/values.yaml").read_text())
    (container,) = _drill_cronjob()["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
    assert container["image"] == values["database"]["cluster"]["imageName"]
