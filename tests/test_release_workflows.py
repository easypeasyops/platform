"""The two release workflows and their tags.

The service chart is released by release-chart.yml from a service/vX.Y.Z tag; a tool that
lives in this repo (tools/<app>/) by release.yml from its <app>/vX.Y.Z tag. A tag must
start exactly one of them, and the admission policy must accept release.yml's identity at
a tool's tag and nothing else from this repo.
"""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github/workflows"
SEMVER = "v[0-9]+.[0-9]+.[0-9]+"


def _tags(workflow: str) -> list[str]:
    doc: dict[Any, Any] = yaml.safe_load((WORKFLOWS / workflow).read_text())
    tags: list[str] = doc[True]["push"]["tags"]  # YAML 1.1 reads the key `on` as true
    return tags


def test_a_tag_starts_exactly_one_release_workflow() -> None:
    chart = f"service/{SEMVER}"
    assert _tags("release-chart.yml") == [chart]
    # Every <app>/vX.Y.Z but the chart's; GitHub applies the patterns in order.
    assert _tags("release.yml") == [f"*/{SEMVER}", f"!{chart}"]


def test_the_admission_policy_accepts_a_tools_release_and_not_the_charts() -> None:
    policy = yaml.safe_load((ROOT / "deploy/policies/require-release-provenance.yaml").read_text())
    identity = policy["spec"]["attestors"][0]["cosign"]["keyless"]["identities"][0]
    signer = re.compile(identity["subjectRegExp"])
    base = "https://github.com/easypeasyops/platform/.github/workflows"
    assert signer.match(f"{base}/release.yml@refs/tags/servicemonitor/v0.1.0")
    assert not signer.match(f"{base}/release-chart.yml@refs/tags/service/v1.6.0")
    assert not signer.match(f"{base}/ci.yml@refs/heads/main")
