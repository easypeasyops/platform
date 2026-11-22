"""The weekly GKE run (PT-120): .github/workflows/gke-e2e.yml and scripts/gke-run.sh.

Nothing here calls GCP. The workflow is dormant until the foundation's repository
variables exist; once awake, a failed or cancelled run still tears down, in order (what
Kubernetes made in GCP, then Terraform's own, then a check that nothing is left). A
workflow that acts as CI in GCP must be one the foundation's provider admits.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github/workflows"


def _workflow(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text())
    return loaded


def _gcp_workflows() -> set[str]:
    return {
        p.name
        for p in WORKFLOWS.glob("*.yml")
        if "uses: google-github-actions/auth@" in p.read_text()
    }


def test_every_workflow_acting_in_gcp_is_admitted_by_the_foundation() -> None:
    """The provider admits a token only from the workflows in ci_workflows (wif.tf): one
    missing there fails auth with "rejected by the attribute condition"."""
    variables = (ROOT / "infra/gcp/foundation/variables.tf").read_text()
    block = variables[variables.index('variable "ci_workflows"') :]
    default = re.search(r"default\s*=\s*\[([^\]]*)\]", block)
    assert default
    assert set(re.findall(r'"([^"]+)"', default[1])) == _gcp_workflows()


@pytest.mark.parametrize("name", sorted(_gcp_workflows()))
def test_gcp_workflows_sleep_until_the_foundation_is_bootstrapped(name: str) -> None:
    for job in _workflow(name)["jobs"].values():
        assert job["if"] == "vars.GCP_WORKLOAD_IDENTITY_PROVIDER != ''"


def test_the_run_tears_down_in_order_whatever_happened() -> None:
    steps = _workflow("gke-e2e.yml")["jobs"]["gke"]["steps"]
    names = [s.get("name", s.get("uses", "")) for s in steps]
    teardown = ["Drain what Kubernetes made in GCP", "Destroy the cluster", "Nothing left in GCP"]
    at = [names.index(n) for n in teardown]
    assert at == sorted(at)
    assert at[0] > names.index("The apps, as kind's e2e checks them")
    for i in at:
        assert steps[i]["if"].startswith("always() && ")
    assert "scripts/gke-run.sh drain" in steps[at[0]]["run"]
    assert "terraform destroy" in steps[at[1]]["run"]
    assert "scripts/gke-run.sh leftovers" in steps[at[2]]["run"]


def _gke_run(
    tmp_path: Path, *args: str, listed: dict[str, str] | None = None, refused: str = ""
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """scripts/gke-run.sh with a fake gcloud: `listed` maps the start of a list command
    ("compute disks list") to what it prints, the others print nothing; `refused` is one
    that fails. Returns the run and the delete commands gcloud was given."""
    calls = tmp_path / "deletes"
    cases = "".join(f'  "{k}"*) echo "{v}" ;;\n' for k, v in (listed or {}).items())
    if refused:
        cases = f'  "{refused}"*) echo refused >&2; exit 1 ;;\n' + cases
    gcloud = tmp_path / "gcloud"
    deletes = f'  *" delete "*|"storage rm "*) echo "$*" >>{calls} ;;\n'
    gcloud.write_text(f'#!/usr/bin/env bash\ncase "$*" in\n{deletes}{cases}  *) ;;\nesac\n')
    gcloud.chmod(0o755)
    run = subprocess.run(
        [ROOT / "scripts/gke-run.sh", *args],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "STATE_BUCKET": "p-tfstate"},
        capture_output=True,
        text=True,
        check=False,
    )
    return run, calls.read_text().splitlines() if calls.exists() else []


LEFTOVERS = ("leftovers", "gh-1-1", "2026-11-21T00:00:00Z")


def test_nothing_left_passes(tmp_path: Path) -> None:
    run, _ = _gke_run(tmp_path, *LEFTOVERS)
    assert run.returncode == 0, run.stderr
    assert "run gh-1-1 left nothing" in run.stdout


def test_a_leftover_disk_fails_and_is_named(tmp_path: Path) -> None:
    run, _ = _gke_run(tmp_path, *LEFTOVERS, listed={"compute disks list": "pvc-0123"})
    assert run.returncode == 1
    assert "disk pvc-0123" in run.stdout
    assert "left 1 resources in GCP" in run.stderr


DISK = "https://www.googleapis.com/compute/v1/projects/p/zones/us-west1-b/disks/pvc-0123"


def test_the_reaper_deletes_what_a_run_left_and_goes_red(tmp_path: Path) -> None:
    listed = {
        "container clusters list": "platform-gh-1-1 us-west1-b",
        "compute disks list": DISK,
        "storage objects list": "gke/gh-1-1/default.tfstate",
    }
    run, deletes = _gke_run(tmp_path, "reap", listed=listed)
    assert run.returncode == 1
    assert "reaped 3 resources" in run.stderr
    assert deletes == [
        "container clusters delete platform-gh-1-1 --location us-west1-b --quiet",
        f"compute disks delete {DISK} --quiet",
        "storage rm gs://p-tfstate/gke/gh-1-1/default.tfstate --quiet",
    ]


def test_a_dry_run_deletes_nothing(tmp_path: Path) -> None:
    run, deletes = _gke_run(tmp_path, "reap", "--dry-run", listed={"compute disks list": DISK})
    assert run.returncode == 0, run.stderr
    assert f"reap disks {DISK}" in run.stdout
    assert deletes == []


def test_nothing_to_reap_is_green(tmp_path: Path) -> None:
    run, deletes = _gke_run(tmp_path, "reap")
    assert run.returncode == 0, run.stderr
    assert "nothing older than" in run.stdout
    assert deletes == []


def test_a_refused_list_fails_the_reap_rather_than_reading_as_empty(tmp_path: Path) -> None:
    run, _ = _gke_run(tmp_path, "reap", refused="compute addresses list")
    assert run.returncode != 0
    assert "nothing older than" not in run.stdout
