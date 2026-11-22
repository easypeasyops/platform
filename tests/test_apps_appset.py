"""The apps' ApplicationSet in the platform chart (components.apps; PT-119).

Rendered from files/applicationset.yaml as it is, so that where the apps root applied it
(apps/applicationset.yaml), Argo CD adopts the same object and no app's Application is
regenerated. A cluster's own values (cluster.api_server, cluster.bucket) reach every app as
its Application's valuesObject; kind passes none, and the object stays byte for byte.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "deploy/argocd/platform"
FILE = CHART / "files/applicationset.yaml"


def _appsets(*args: str) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        if os.environ.get("CI"):
            pytest.fail("helm is required in CI")
        pytest.skip("helm not installed")
    out = subprocess.run(
        ["helm", "template", "platform", str(CHART), "--set", "arch=amd64", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d and d["kind"] == "ApplicationSet"]


def test_the_chart_s_copy_is_the_apps_root_s_file() -> None:
    assert FILE.read_bytes() == (ROOT / "apps/applicationset.yaml").read_bytes()


def test_off_by_default_and_on_it_is_the_file_as_it_is() -> None:
    assert _appsets() == []
    assert _appsets("--set", "components.apps=true") == [yaml.safe_load(FILE.read_text())]


def test_a_cluster_s_values_reach_every_app_over_its_own() -> None:
    cluster = {
        "api_server": {"cidrs": ["10.0.0.2/32"], "port": 443},
        "bucket": {"url": "gs://b/", "endpoint": None, "credentials": "workloadIdentity"},
    }
    (appset,) = _appsets(
        "--set", "components.apps=true", "--set-json", f"cluster={json.dumps(cluster)}"
    )
    original = yaml.safe_load(FILE.read_text())
    patch = appset["spec"].pop("templatePatch")
    original_patch = original["spec"].pop("templatePatch")
    assert appset == original  # only the template patch differs
    (line,) = [line for line in patch.splitlines() if "valuesObject:" in line]
    assert patch.replace(line + "\n", "") == original_patch
    raw = line.split("valuesObject: ", 1)[1]
    # The ApplicationSet's own template fills these two; nothing else in it is a template.
    assert raw.count("{{") == 2
    filled = raw.replace("{{ .app }}", "aipostit").replace("{{ .env }}", "prod")
    assert json.loads(filled) == {
        "networkPolicy": {
            "apiServerTo": [{"ipBlock": {"cidr": "10.0.0.2/32"}}],
            "apiServerPort": 443,
        },
        "database": {
            "cluster": {
                "backup": {
                    "credentials": "workloadIdentity",
                    "destinationPath": "gs://b/aipostit-prod",
                }
            }
        },
    }
