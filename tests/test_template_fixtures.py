"""The service chart fixtures charts/service/tests/<app>/ for app-template's apps (PT-76) are
the template's own deploy values, byte for byte, at TEMPLATE_COMMIT. So the goldens render
exactly what a new app made from the template deploys, and a chart change that breaks it
fails here, not on the template's next Renovate PR after apps have copied the break.

When the template's deploy values change: move TEMPLATE_COMMIT, copy its files in, and
regenerate the goldens (UPDATE=1 scripts/chart-golden.sh), in one PR here.
"""

import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_REPO = "easypeasyops/app-template"
TEMPLATE_COMMIT = "a41a14ae6577fa7f6f282bf276076391fc7719ab"
APPS = ["python-template", "java-template"]
# Fixture file (charts/service/tests/<app>/) -> the template's file (apps/<app>/deploy/).
FILES = {
    "values.yaml": "service/values.yaml",
    "slo-rules.values.yaml": "service/slo-rules.values.yaml",
    "staging-values.yaml": "envs/staging.yaml",
    "prod-values.yaml": "envs/prod.yaml",
}


@pytest.mark.integration
@pytest.mark.parametrize("app", APPS)
@pytest.mark.parametrize("fixture", FILES)
def test_the_fixture_is_the_template_at_its_pinned_commit(app: str, fixture: str) -> None:
    url = (
        f"https://raw.githubusercontent.com/{TEMPLATE_REPO}/{TEMPLATE_COMMIT}"
        f"/apps/{app}/deploy/{FILES[fixture]}"
    )
    with urllib.request.urlopen(url, timeout=30) as response:
        template = response.read()
    assert (ROOT / "charts/service/tests" / app / fixture).read_bytes() == template, url
