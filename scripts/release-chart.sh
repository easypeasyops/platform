#!/usr/bin/env bash
# Release the service chart as an OCI artifact (.github/workflows/release-chart.yml runs it
# on a service/vX.Y.Z tag). Apps pin the chart by the digest this prints.
#
#   scripts/release-chart.sh check <tag>   # stop unless the tag may become a release
#   scripts/release-chart.sh publish       # package, push, prove the push; prints the digest
#
#   REGISTRY   where charts go (default ghcr.io/easypeasyops/charts)
#   PLAIN_HTTP=1  for a local test registry
#   GITHUB_OUTPUT  when set (Actions), version/ref/digest are written there too
#
# `check` refuses a release unless: the tag is service/v<Chart.yaml's version>; the tagged
# commit is on origin/main (reviewed and green); the goldens still match; and that version
# is NOT already in the registry. OCI tags can be overwritten, and an app that pinned 0.3.0
# must never get different bytes under that name. "Already there" fails closed: anything but
# a clear not-found from the registry stops the release.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHART="$ROOT/charts/service"
REGISTRY="${REGISTRY:-ghcr.io/easypeasyops/charts}"
helm_oci=()
if [[ "${PLAIN_HTTP:-0}" == 1 ]]; then helm_oci+=(--plain-http); fi

version() { sed -nE 's/^version: *"?([^"[:space:]]+)"?.*/\1/p' "$CHART/Chart.yaml"; }
output() { if [[ -n "${GITHUB_OUTPUT:-}" ]]; then echo "$1=$2" >>"$GITHUB_OUTPUT"; fi; }
die() { echo "::error::$*" >&2; exit 1; }

check() {
  local tag="$1" v
  v=$(version)
  [[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Chart.yaml version '$v' is not X.Y.Z"
  [[ "$tag" == "service/v$v" ]] || die "tag $tag does not match Chart.yaml version $v (want service/v$v)"
  git -C "$ROOT" merge-base --is-ancestor HEAD origin/main ||
    die "the tagged commit $(git -C "$ROOT" rev-parse --short HEAD) is not on main: tag a merged commit"
  "$ROOT/scripts/chart-golden.sh" >/dev/null || die "the service chart's goldens don't match"

  local out
  if out=$(helm show chart "oci://$REGISTRY/service" --version "$v" "${helm_oci[@]}" 2>&1); then
    die "service $v is already in $REGISTRY: releases are immutable, bump the version"
  elif ! grep -qE ': (not found|manifest unknown|name unknown)' <<<"$out"; then
    die "couldn't confirm service $v is new in $REGISTRY: $out"
  fi
  echo "ok: service $v from $(git -C "$ROOT" rev-parse --short HEAD) may be released to $REGISTRY"
}

publish() {
  local v dist out digest
  v=$(version)
  dist=$(mktemp -d)
  trap 'rm -rf "$dist"' RETURN
  helm package "$CHART" --destination "$dist" >/dev/null
  local pkg="$dist/service-$v.tgz"

  out=$(helm push "$pkg" "oci://$REGISTRY" "${helm_oci[@]}" 2>&1) || die "helm push failed: $out"
  echo "$out"
  digest=$(sed -nE 's/^Digest: (sha256:[0-9a-f]{64})$/\1/p' <<<"$out")
  [[ -n "$digest" ]] || die "no digest in helm push's output"

  # Prove the push: pulled back BY DIGEST, the chart is byte for byte what was packaged.
  mkdir "$dist/pulled"
  helm pull "oci://$REGISTRY/service@$digest" --destination "$dist/pulled" "${helm_oci[@]}" >/dev/null
  local pulled=("$dist"/pulled/*.tgz) # named service@sha256-<digest>.tgz
  [[ ${#pulled[@]} == 1 && -f "${pulled[0]}" ]] || die "pulling $digest gave no single chart"
  cmp "$pkg" "${pulled[0]}" || die "the chart pulled by digest differs from the one pushed"

  echo "ok: oci://$REGISTRY/service:$v = $digest"
  output version "$v"
  output ref "$REGISTRY/service"
  output digest "$digest"
}

case "${1:-}" in
  check) check "${2:?usage: $0 check <tag>}" ;;
  publish) publish ;;
  *) echo "usage: $0 check <tag> | publish" >&2; exit 2 ;;
esac
