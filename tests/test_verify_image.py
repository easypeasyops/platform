"""scripts/verify-image.sh: the deploy gate's provenance check, the same rule as the
admission policy (deploy/policies/require-release-provenance.yaml; tests/test_admission_policies.py
checks they agree). An app's deploy script calls it for every registry image.

Unit tests run it against a stub gh that prints a canned attestation. Integration tests run
it against the real registry and Sigstore, on a real release (RELEASE).
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VERIFY = ROOT / "scripts/verify-image.sh"
DIGEST = "a" * 64
IMAGE = f"ghcr.io/easypeasyops/links/shortly@sha256:{DIGEST}"  # ghcr.io/<owner>/<repo>/<app>
# shortly v0.7.2, released by easypeasyops/links' release.yml at shortly/v0.7.2.
RELEASE = "ghcr.io/easypeasyops/links/shortly@sha256:" + (
    "656822ee670d160ebde14c40308a411c9b3d1f1aa18d0935fb6cc580ddafa950"
)


def _attestation(ref: str, digest: str = DIGEST) -> str:
    """What `gh attestation verify --format json` prints, trimmed to what is checked."""
    cert = {
        "sourceRepositoryRef": ref,
        "buildSignerURI": f"https://github.com/easypeasyops/links/.github/workflows/release.yml@{ref}",
        "sourceRepositoryURI": "https://github.com/easypeasyops/links",
        "sourceRepositoryDigest": "0123456789abcdef",
    }
    statement = {"subject": [{"digest": {"sha256": digest}}]}
    return json.dumps(
        [{"verificationResult": {"signature": {"certificate": cert}, "statement": statement}}]
    )


def _verify(tmp_path: Path, ref: str, gh_output: str | None) -> subprocess.CompletedProcess[str]:
    """verify-image.sh with a stub gh on PATH: prints gh_output, or fails when it is None."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    body = "echo denied >&2; exit 1" if gh_output is None else "printf '%s\\n' \"$FAKE_GH\""
    gh.write_text(f"#!/usr/bin/env bash\n{body}\n")
    gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_GH": gh_output or ""}
    return subprocess.run([str(VERIFY), ref], capture_output=True, text=True, env=env)


def test_a_release_tag_build_of_this_digest_verifies(tmp_path: Path) -> None:
    result = _verify(tmp_path, IMAGE, _attestation("refs/tags/shortly/v0.7.2"))
    assert result.returncode == 0, result.stderr
    assert "verified: built by" in result.stdout


@pytest.mark.parametrize(
    ("image", "ref", "ok"),
    [
        (IMAGE, "refs/tags/shortly/v0.7.2", True),
        (IMAGE, "refs/tags/pager/v0.1.0", False),  # another app of the repo
        (IMAGE, "refs/tags/v0.7.2", False),  # a plain tag: no app
        (IMAGE, "refs/tags/shortly/v0.7.2-rc1", False),
    ],
)
def test_an_app_image_needs_its_own_apps_tag(
    tmp_path: Path, image: str, ref: str, ok: bool
) -> None:
    """ghcr.io/<owner>/<repo>/<app> is released from <app>/vX.Y.Z (platform plan 3.5), so one
    app of a repo can't publish as another."""
    result = _verify(tmp_path, image, _attestation(ref))
    assert (result.returncode == 0) is ok, result.stderr
    if not ok:
        assert "not from a release tag" in result.stderr


def test_no_valid_provenance_is_refused(tmp_path: Path) -> None:
    result = _verify(tmp_path, IMAGE, None)
    assert result.returncode != 0
    assert "REFUSED: no valid provenance" in result.stderr


def test_a_build_from_a_branch_is_refused(tmp_path: Path) -> None:
    # Signed by release.yml, but from refs/heads/...: not a release.
    result = _verify(tmp_path, IMAGE, _attestation("refs/heads/main"))
    assert result.returncode != 0
    assert "not from a release tag" in result.stderr


def test_an_attestation_for_another_digest_is_refused(tmp_path: Path) -> None:
    result = _verify(tmp_path, IMAGE, _attestation("refs/tags/shortly/v0.7.2", digest="b" * 64))
    assert result.returncode != 0


@pytest.mark.parametrize(
    ("ref", "reason"),
    [
        ("ghcr.io/easypeasyops/links/shortly:0.7.2", "is not a digest reference"),
        (
            "docker.io/easypeasyops/links@sha256:" + "a" * 64,
            "is not a ghcr.io/<owner>/<repo>/<app> image",
        ),
        ("ghcr.io/evil/links/shortly@sha256:" + "a" * 64, "is not one of easypeasyops's images"),
        # The old owner and form (shortly up to v0.7.1), refused since platform plan 3.5.
        (
            "ghcr.io/statsguy/shortly@sha256:" + "a" * 64,
            "is not a ghcr.io/<owner>/<repo>/<app> image",
        ),
        (
            "ghcr.io/statsguy/links/shortly@sha256:" + "a" * 64,
            "is not one of easypeasyops's images",
        ),
        (
            "ghcr.io/easypeasyops/links@sha256:" + "a" * 64,
            "is not a ghcr.io/<owner>/<repo>/<app> image",
        ),
        (
            "ghcr.io/easypeasyops/a/b/c@sha256:" + "a" * 64,
            "is not a ghcr.io/<owner>/<repo>/<app> image",
        ),
    ],
)
def test_images_no_repo_of_ours_can_vouch_for_are_refused(ref: str, reason: str) -> None:
    """Keyed by identity (platform plan 1.5): the image's name says which repo must have
    built it, and that repo must be one of ours. Refused before any network call."""
    result = subprocess.run([str(VERIFY), ref], capture_output=True, text=True)
    assert result.returncode == 1
    assert reason in result.stderr


# ------------------------------------------------------------------- real registry


def _real_gh() -> None:
    if shutil.which("gh") is None:
        if os.environ.get("CI"):
            pytest.fail("gh is required for the provenance tests in CI")
        pytest.skip("gh not installed")


def _real(**env: str) -> subprocess.CompletedProcess[str]:
    _real_gh()
    return subprocess.run(
        [str(VERIFY), RELEASE], capture_output=True, text=True, env={**os.environ, **env}
    )


@pytest.mark.integration
def test_a_real_release_verifies() -> None:
    result = _real()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "refs/tags/shortly/v" in result.stdout


@pytest.mark.integration
def test_a_real_release_is_refused_for_the_wrong_signer() -> None:
    result = _real(VERIFY_SIGNER="easypeasyops/links/.github/workflows/ci.yml")
    assert result.returncode != 0
    assert "REFUSED" in result.stderr


@pytest.mark.integration
def test_a_real_release_is_refused_as_another_repos_image() -> None:
    """The release's provenance names easypeasyops/links: claimed for another repo, it fails."""
    result = _real(VERIFY_REPO="easypeasyops/oncall")
    assert result.returncode != 0
    assert "REFUSED" in result.stderr
