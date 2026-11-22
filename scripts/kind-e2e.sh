#!/usr/bin/env bash
# One app in one environment, deployed on the platform it pins and checked: the work of
# .github/workflows/reusable-kind-e2e.yml, which runs it in CI after `scripts/cluster.sh up`
# (ARGOCD_APPS=0) on a fresh kind cluster, from this repo at the app's platform.lock commit.
#
#   CHART=oci://ghcr.io/easypeasyops/charts/service@sha256:... IMAGE=aipostit:e2e \
#     APP_REPO=<the app repo's checkout> scripts/kind-e2e.sh aipostit staging
#
# As Argo CD would deploy it, without Argo CD:
#   - the namespace <app>-<env> with the apps ApplicationSet's labels and the
#     deploy/app-namespace/ baseline (both read from this checkout, so they can't drift);
#   - a placeholder for every key the app's pods read from its store, loaded with
#     scripts/load-app-env.sh (the keys are the rendered secretKeyRefs into the chart's
#     <release>-env Secret: no per-app list);
#   - the chart at CHART with the values files apps/<app>/envs/<env>.yaml lists, in its
#     order, from APP_REPO, then deploy/kind/e2e-values.yaml; the image is the local IMAGE.
#     For an app the platform doesn't deploy (app-template in the platform's own CI), there
#     is no env file: VALUES_FILES lists them instead (paths in APP_REPO, in order, separated
#     by spaces or newlines). Both at once is an error: one list, never a silent choice.
# Then checks (scripts/lib/e2e-checks.sh, which the weekly GKE run shares): every
# Deployment, StatefulSet and Rollout Ready; the front door routes each HTTPRoute hostname
# to the app; Prometheus scrapes every target of each ServiceMonitor and PodMonitor, all
# up; Prometheus has loaded each PrometheusRule's groups. On failure it prints the
# namespace's state.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# The front door's users port on the runner (scripts/cluster.sh, FRONT_DOOR_PORTS).
FRONT_DOOR=http://127.0.0.1:8080
# e2e_checks, e2e_diagnostics, pass, die
# shellcheck source=scripts/lib/e2e-checks.sh
source "$ROOT/scripts/lib/e2e-checks.sh"

[[ $# -eq 2 ]] || die "usage: $0 <app> <env>"
app=$1 env=$2 ns="$1-$2"
: "${CHART:?set CHART, the service chart ref the app renders with}"
: "${IMAGE:?set IMAGE, the app image loaded into kind (name:tag)}"
: "${APP_REPO:?set APP_REPO, the app repo checkout its values files are read from}"
# It replaces the app's keys in the cluster's store with placeholders: never on the laptop,
# whose cluster has the same name (kind-shortly) and the real keys.
[[ "${GITHUB_ACTIONS:-}" == true ]] || die "CI only: it overwrites $app's keys in the secret store"

envfile="$ROOT/apps/$app/envs/$env.yaml"
values=()
if [[ -f "$envfile" ]]; then
  [[ -z "${VALUES_FILES:-}" ]] || die "apps/$app/envs/$env.yaml lists $app's values files: unset VALUES_FILES"
  while read -r f; do values+=(--values "$APP_REPO/$f"); done < <(yq -r '.values.files[]' "$envfile")
elif [[ -n "${VALUES_FILES:-}" ]]; then
  read -r -d '' -a files <<<"$VALUES_FILES" || true
  for f in "${files[@]}"; do values+=(--values "$APP_REPO/$f"); done
else
  die "no apps/$app/envs/$env.yaml (the platform at this commit doesn't run $app in $env) and no VALUES_FILES"
fi
values+=(--values "$ROOT/deploy/kind/e2e-values.yaml")
# A tag, not the env file's release digest: a digest wins over a tag, so clear it.
image=(--set "image.repository=${IMAGE%:*}" --set "image.tag=${IMAGE##*:}" --set "image.digest=")
rendered=$(mktemp)

diagnostics() {
  [[ $? -ne 0 ]] || return 0
  e2e_diagnostics "$ns" "$app"
}
trap diagnostics EXIT

# ---------------------------------------------------------------- namespace
# The ApplicationSet's managedNamespaceMetadata labels, {{ .app }} and {{ .env }} filled in.
labels=()
while read -r l; do
  l=${l//'{{ .app }}'/$app} l=${l//'{{ .env }}'/$env}
  [[ "$l" != *'{{'* ]] || die "apps/applicationset.yaml: a namespace label this script can't fill: $l"
  labels+=("$l")
done < <(yq -r '.spec.template.spec.syncPolicy.managedNamespaceMetadata.labels | to_entries[] | .key + "=" + .value' \
  "$ROOT/apps/applicationset.yaml")
kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl label namespace "$ns" --overwrite "${labels[@]}" >/dev/null
kubectl -n "$ns" apply -f "$ROOT/deploy/app-namespace/" >/dev/null
pass "namespace $ns as the apps ApplicationSet makes it (${labels[*]}; deploy/app-namespace/)"

# ---------------------------------------------------------------- keys
helm template "$app" "$CHART" --namespace "$ns" "${values[@]}" "${image[@]}" >"$rendered"
secret=$(yq -N -r 'select(.kind == "ExternalSecret") | .spec.target.name' "$rendered")
if [[ -n "$secret" ]]; then
  keys=$(yq -N -r '.. | select(tag == "!!map" and has("secretKeyRef")) | .secretKeyRef
    | select(.name == "'"$secret"'") | .key' "$rendered" | sort -u)
  [[ -n "$keys" ]] || die "$app has secretEnv but its pods read no key of $secret: nothing to load"
  dotenv=$(mktemp)
  for k in $keys; do echo "$k=e2e-placeholder"; done >"$dotenv"
  "$ROOT/scripts/load-app-env.sh" "$app" "$dotenv"
  pass "placeholders for $app's keys in its store: $(echo "$keys" | wc -l | tr -d ' ') keys"
else
  echo "skip keys: $app sets no secretEnv"
fi

# ---------------------------------------------------------------- deploy
helm upgrade --install "$app" "$CHART" --namespace "$ns" "${values[@]}" "${image[@]}" \
  --wait --timeout 5m >/dev/null
pass "$app installed in $ns: $CHART with ${values[*]//--values /}"

# ---------------------------------------------------------------- checks
e2e_checks "$ns" "$FRONT_DOOR"

pass "$app in $env on the platform at this commit"
