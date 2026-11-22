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


def _leftovers(tmp_path: Path, listed: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """scripts/gke-run.sh leftovers with a fake gcloud: `listed` maps a command
    ("compute disks") to what its list prints; the others print nothing."""
    gcloud = tmp_path / "gcloud"
    cases = "".join(f'  "{k} list") echo "{v}" ;;\n' for k, v in listed.items())
    gcloud.write_text(f'#!/usr/bin/env bash\ncase "$1 $2 $3" in\n{cases}  *) ;;\nesac\n')
    gcloud.chmod(0o755)
    return subprocess.run(
        [ROOT / "scripts/gke-run.sh", "leftovers", "gh-1-1", "2026-11-21T00:00:00Z"],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )


def test_nothing_left_passes(tmp_path: Path) -> None:
    run = _leftovers(tmp_path, {})
    assert run.returncode == 0, run.stderr
    assert "run gh-1-1 left nothing" in run.stdout


def test_a_leftover_disk_fails_and_is_named(tmp_path: Path) -> None:
    run = _leftovers(tmp_path, {"compute disks": "pvc-0123"})
    assert run.returncode == 1
    assert "disk pvc-0123" in run.stdout
    assert "left 1 resources in GCP" in run.stderr
