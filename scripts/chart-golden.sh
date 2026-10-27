#!/usr/bin/env bash
# Golden manifests for the service chart: what it renders for each app in
# charts/service/tests/<app>/, checked in beside the values as <env>.golden.yaml. Layered as
# an app's Argo CD Application will: values.yaml (what every environment shares), then any
# generated <name>.values.yaml (such as gen_slo_rules.py's), then <env>-values.yaml.
#
#   scripts/chart-golden.sh            # fail, with a diff, if a render differs from its golden
#   UPDATE=1 scripts/chart-golden.sh   # rewrite the golden files after an intended change
#
# Argo CD applies what `helm template` renders, so the render is the chart's behaviour. A
# change to a template shows up in review as a diff of what the cluster will receive, and a
# refactor must leave every golden file byte for byte the same.
#
# shortly's fixtures are links' deploy/envs/<env>.yaml; its goldens matched links'
# tests/golden/ when the chart was copied, except the chart label (service-, not shortly-).
#
# Normalised so routine changes leave the files alone: the image digest (promotions change
# it) and the chart version in the helm.sh/chart label (releases bump it).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHART="$ROOT/charts/service"
PLACEHOLDER_DIGEST="sha256:$(printf '0%.0s' {1..64})"

values_args() { # <app> <env>
  local dir="$CHART/tests/$1" f
  for f in "$dir/values.yaml" "$dir"/*.values.yaml; do
    if [[ -f "$f" ]]; then printf '%s\n' --values "$f"; fi
  done
  printf '%s\n' --values "$CHART/tests/$1/$2-values.yaml"
}

render() { # <app> <env>
  local args
  mapfile -t args < <(values_args "$1" "$2")
  helm template "$1" "$CHART" \
    --namespace "$1-$2" \
    "${args[@]}" \
    --set "image.digest=$PLACEHOLDER_DIGEST" \
    --kube-version 1.37.0 \
    --api-versions monitoring.coreos.com/v1/ServiceMonitor \
    --api-versions monitoring.coreos.com/v1/PrometheusRule |
    sed -E 's/(helm\.sh\/chart: service-)[^ ]+/\1<version>/'
}

if [[ "${1:-}" == --values-args ]]; then values_args "$2" "$3"; exit; fi # for CI's lint

status=0
for values in "$CHART"/tests/*/*-values.yaml; do
  app=$(basename "$(dirname "$values")")
  env=$(basename "$values" -values.yaml)
  golden="$CHART/tests/$app/$env.golden.yaml"
  if [[ "${UPDATE:-0}" == 1 ]]; then
    render "$app" "$env" >"$golden"
    echo "wrote ${golden#"$ROOT"/}"
  elif ! diff -u --label "golden: $app $env" --label "rendered: $app $env" "$golden" <(render "$app" "$env"); then
    echo "::error::$app $env renders differently from ${golden#"$ROOT"/} (UPDATE=1 $0 after an intended change)"
    status=1
  fi
done
exit "$status"
