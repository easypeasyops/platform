"""Profiles (deploy/profiles/<name>/, DESIGN: Profiles): a cluster's values over kind's.

Argo CD ignores a profile's missing file (ignoreMissingValueFiles), so a misnamed one would
be ignored silently: every file must be one a chart child layers, and platform.yaml may set
only values the platform chart has. The gke profile names no StorageClass, so each claim
gets the cluster's default (cluster contract, default StorageClass).
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.test_platform import ALL_ON, _render, _sources

ROOT = Path(__file__).resolve().parent.parent
PROFILES = sorted(p for p in (ROOT / "deploy/profiles").iterdir() if p.is_dir())
CHART_VALUES = yaml.safe_load((ROOT / "deploy/argocd/platform/values.yaml").read_text())


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def _merge(base: Any, over: Any) -> Any:
    """Helm's layering of two values files: maps merge, a null deletes, the rest replaces."""
    if not (isinstance(base, dict) and isinstance(over, dict)):
        return over
    merged = dict(base)
    for key, value in over.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = _merge(base.get(key), value)
    return merged


def _keys(values: dict[str, Any], prefix: str = "") -> set[str]:
    out = set()
    for key, value in values.items():
        out.add(prefix + key)
        if isinstance(value, dict):
            out |= _keys(value, f"{prefix}{key}.")
    return out


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.name)
def test_every_file_is_one_a_chart_layers(profile: Path) -> None:
    overlays = {
        Path(f).name
        for app in _render(*ALL_ON, f"profile={profile.name}")
        for f in _sources(app)[0].get("helm", {}).get("valueFiles", [])[1:]
    }
    files = {f.name for f in profile.iterdir()} - {"platform.yaml"}
    assert files <= overlays, files - overlays


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.name)
def test_platform_yaml_sets_only_values_the_chart_has(profile: Path) -> None:
    own = _load(profile / "platform.yaml")
    assert _keys(own) <= _keys(CHART_VALUES), _keys(own) - _keys(CHART_VALUES)


def _storage_classes(values: Any) -> list[str]:
    if isinstance(values, dict):
        found = [values["storageClassName"]] if "storageClassName" in values else []
        return found + [c for v in values.values() for c in _storage_classes(v)]
    if isinstance(values, list):
        return [c for v in values for c in _storage_classes(v)]
    return []


def test_gke_names_no_storage_class_and_changes_nothing_else_in_the_claims() -> None:
    gke = ROOT / "deploy/profiles/gke"
    for name in ("monitoring-values.yaml", "victoria-logs-values.yaml"):
        kind = _load(ROOT / "deploy/kind" / name)
        assert _storage_classes(kind), name  # kind names its class
        assert _storage_classes(_merge(kind, _load(gke / name))) == [], name
    kind_manifests = _load(ROOT / "deploy/kind/monitoring-values.yaml")["extraManifests"]
    for manifest in kind_manifests:
        manifest["spec"].pop("storageClassName", None)
    assert _load(gke / "monitoring-values.yaml")["extraManifests"] == kind_manifests
