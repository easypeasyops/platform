#!/usr/bin/env bash
# Verify that an image was built by its own repo's release workflow from a release tag, before
# anything deploys it. Keyed by identity: the image's name says which repo, and which app's
# tags, must have built it. Same rule as the admission policy
# (deploy/policies/require-release-provenance.yaml; tests/test_admission_policies.py checks
# they agree). An app's deploy script calls this for every registry image; it also runs on
# its own:
#
#   scripts/verify-image.sh ghcr.io/easypeasyops/links/shortly@sha256:...
#
# Two forms of image name (platform plan 3.5, expand / contract):
#   - ghcr.io/<owner>/<repo>/<app>: an app in a domain repo (apps/<app>/), owner VERIFY_OWNER
#     (default easypeasyops), released from a <app>/vX.Y.Z tag
#   - ghcr.io/<owner>/<repo>: the old form, owner VERIFY_LEGACY_OWNER (default statsguy),
#     released from a vX.Y.Z tag; shortly's releases up to v0.7.1. Removed once nothing
#     deploys them.
# What is checked (via the signed SLSA provenance from actions/attest-build-provenance):
#   - the signature chains to Sigstore's public-good root and the GitHub Actions OIDC issuer
#   - the signer is <owner>/<repo>/.github/workflows/release.yml (not CI, not a fork, not
#     another repo's release)
#   - the build ran from that app's release tag (refs/tags/<app>/vX.Y.Z, or refs/tags/vX.Y.Z
#     for the old form): a release, not a branch, and not another app's release
#   - the attestation's subject is exactly this digest
# Exit 0 only if all hold. Fails closed: no gh, no network, or no attestation = no deploy.
set -euo pipefail

REF="${1:?usage: $0 <registry/image@sha256:digest>}"
OWNER="${VERIFY_OWNER:-easypeasyops}"
LEGACY_OWNER="${VERIFY_LEGACY_OWNER:-statsguy}"

fail() { echo "REFUSED: $*" >&2; exit 1; }

[[ "$REF" == *@sha256:* ]] || fail "$REF is not a digest reference; tags are mutable, so only a digest can be verified"
# The image's name says which repo must have built it, and from which tags.
if [[ "${REF%@*}" =~ ^ghcr\.io/([^/]+)/([^/]+)/([^/]+)$ ]]; then
  [[ "${BASH_REMATCH[1]}" == "$OWNER" ]] || fail "$REF is not one of $OWNER's images"
  TAGS="refs/tags/${BASH_REMATCH[3]}/v"
elif [[ "${REF%@*}" =~ ^ghcr\.io/([^/]+)/([^/]+)$ ]]; then
  [[ "${BASH_REMATCH[1]}" == "$LEGACY_OWNER" ]] || fail "$REF is not one of $LEGACY_OWNER's images"
  TAGS="refs/tags/v"
else
  fail "$REF is not a ghcr.io/<owner>/<repo>/<app> image: its source repository can't be known"
fi
REPO="${VERIFY_REPO:-${BASH_REMATCH[1]}/${BASH_REMATCH[2]}}"
SIGNER="${VERIFY_SIGNER:-$REPO/.github/workflows/release.yml}"
command -v gh >/dev/null || fail "gh (GitHub CLI) is required to verify provenance: https://cli.github.com"

# --bundle-from-oci reads the attestation from the registry (release.yml pushes it there),
# so this works without GitHub API access too.
if ! out=$(gh attestation verify "oci://$REF" --repo "$REPO" --signer-workflow "$SIGNER" \
  --predicate-type https://slsa.dev/provenance/v1 --bundle-from-oci --format json 2>&1); then
  fail "no valid provenance from $SIGNER for $REF
$out"
fi

python3 - "$out" "${REF#*@sha256:}" "$TAGS" <<'PY' || exit 1
import json, re, sys

results = json.loads(sys.argv[1])
digest, tags = sys.argv[2], sys.argv[3]
release = re.compile(re.escape(tags) + r"[0-9]+\.[0-9]+\.[0-9]+$")
for r in results:
    v = r["verificationResult"]
    cert = v["signature"]["certificate"]
    ref = cert.get("sourceRepositoryRef", "")
    subjects = {s["digest"].get("sha256") for s in v["statement"]["subject"]}
    if release.match(ref) and digest in subjects:
        print(f"verified: built by {cert['buildSignerURI']}\n"
              f"          from {cert['sourceRepositoryURI']} {ref} @ {cert['sourceRepositoryDigest'][:12]}")
        sys.exit(0)
refs = sorted({r["verificationResult"]["signature"]["certificate"].get("sourceRepositoryRef") for r in results})
print(f"REFUSED: signed by the release workflow, but not from a release tag {tags}X.Y.Z (refs: {refs})", file=sys.stderr)
sys.exit(1)
PY
