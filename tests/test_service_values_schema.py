"""The service chart's values schema (charts/service/values.schema.json, PT-73): a value the
chart doesn't know fails the render, naming the key, instead of being silently ignored. And
the schema keeps up with values.yaml: Helm validates the defaults too, so a value added to
values.yaml but not to the schema fails every render (the goldens included).
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "charts/service"
MINIMAL = ("app.name=a", "image.repository=ghcr.io/easypeasyops/a", "image.tag=t")


def _template(chart: Path, *sets: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("helm") is None:
        if os.environ.get("CI"):
            pytest.fail("helm is required in CI")
        pytest.skip("helm not installed")
    args = [arg for s in (*MINIMAL, *sets) for arg in ("--set", s)]
    return subprocess.run(
        ["helm", "template", "a", str(chart), *args], capture_output=True, text=True
    )


def test_known_values_render() -> None:
    result = _template(CHART, "workers.celery.command={celery}", "env[0].name=X", "env[0].value=1")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("sets", "where", "key"),
    [
        (["autoscalling.enabled=true"], "''", "autoscalling"),
        (["gateway.hostname=a.example"], "'/gateway'", "hostname"),
        (["canary.analysis.maxErrorRate=0.1"], "'/canary/analysis'", "maxErrorRate"),
        (["workers.celery.comand={celery}"], "'/workers/celery'", "comand"),
        (["env[0].name=X", "env[0].valu=1"], "'/env/0'", "valu"),
    ],
)
def test_an_unknown_value_fails_the_render_and_names_it(
    sets: list[str], where: str, key: str
) -> None:
    result = _template(CHART, *sets)
    assert result.returncode != 0
    assert f"at {where}: additional properties '{key}' not allowed" in result.stderr


def test_a_value_added_to_values_yaml_needs_the_schema_too(tmp_path: Path) -> None:
    chart = tmp_path / "service"
    shutil.copytree(CHART, chart, ignore=shutil.ignore_patterns("tests"))
    with (chart / "values.yaml").open("a") as values:
        values.write("\nnewValue: true\n")
    result = _template(chart)
    assert result.returncode != 0
    assert "additional properties 'newValue' not allowed" in result.stderr
