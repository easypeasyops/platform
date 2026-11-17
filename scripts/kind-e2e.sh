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
# Then checks: every Deployment, StatefulSet and Rollout Ready; the front door routes each
# HTTPRoute hostname to the app; Prometheus scrapes every target of each ServiceMonitor and
# PodMonitor, all up; Prometheus has loaded each PrometheusRule's groups. On failure it
# prints the namespace's state.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# The front door's users port on the runner (scripts/cluster.sh, FRONT_DOOR_PORTS).
FRONT_DOOR=http://127.0.0.1:8080
PROM=/api/v1/namespaces/monitoring/services/http:kps-prometheus:9090/proxy

pass() { echo "ok   $* (${SECONDS}s)"; }
die() { echo "FAIL: $*" >&2; exit 1; }

[[ $# -eq 2 ]] || die "usage: $0 <app> <env>"
app=$1 env=$2 ns="$1-$2"
: "${CHART:?set CHART, the service chart ref the app renders with}"
: "${IMAGE:?set IMAGE, the app image loaded into kind (name:tag)}"
: "${APP_REPO:?set APP_REPO, the app repo checkout its values files are read from}"
# It replaces the app's keys in the cluster's store with placeholders: never on the laptop,
# whose cluster has the same name (kind-shortly) and the real keys.
[[ "${GITHUB_ACTIONS:-}" == true ]] || die "CI only: it overwrites $app's keys in the secret store"

envfile="$ROOT/apps/$app/envs/$env.yaml"
[[ -f "$envfile" ]] || die "no apps/$app/envs/$env.yaml: the platform at this commit doesn't run $app in $env"
values=()
while read -r f; do values+=(--values "$APP_REPO/$f"); done < <(yq -r '.values.files[]' "$envfile")
values+=(--values "$ROOT/deploy/kind/e2e-values.yaml")
# A tag, not the env file's release digest: a digest wins over a tag, so clear it.
image=(--set "image.repository=${IMAGE%:*}" --set "image.tag=${IMAGE##*:}" --set "image.digest=")
rendered=$(mktemp)

diagnostics() {
  [[ $? -ne 0 ]] || return 0
  echo "--- $ns ---"
  kubectl -n "$ns" get all,externalsecret,httproute,servicemonitor,podmonitor,prometheusrule -o wide || true
  kubectl -n "$ns" get events --sort-by=.lastTimestamp | tail -30 || true
  kubectl -n "$ns" describe pods | grep -A15 -E '^(Name:|Conditions:|Events:)' | head -150 || true
  kubectl -n "$ns" logs -l "app.kubernetes.io/instance=$app" --all-containers --prefix --tail=30 || true
  kubectl -n "$ns" get externalsecret -o jsonpath='{range .items[*]}{.metadata.name}: {.status.conditions[*].message}{"\n"}{end}' || true
  kubectl -n "$ns" get httproute -o jsonpath='{range .items[*]}{.metadata.name}:{range .status.parents[*].conditions[*]} {.type}={.status} ({.message}){end}{"\n"}{end}' || true
  kubectl -n "$ns" get policyreport -o wide 2>/dev/null || true
  kubectl describe nodes | sed -n '/^Allocated resources:/,/^Events:/p' || true
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

# ---------------------------------------------------------------- 1. workloads ready
workloads=$(kubectl -n "$ns" get deployment,statefulset -o name)
for w in $workloads; do kubectl -n "$ns" rollout status "$w" --timeout=180s >/dev/null; done
rollouts=$(kubectl -n "$ns" get rollouts.argoproj.io -o name 2>/dev/null || true)
for r in $rollouts; do
  kubectl -n "$ns" wait "$r" --for=jsonpath='{.status.phase}'=Healthy --timeout=300s >/dev/null
done
all=$(echo "$workloads $rollouts" | xargs)
[[ -n "$all" ]] || die "no Deployment, StatefulSet or Rollout in $ns"
pass "ready: $all"

# ---------------------------------------------------------------- 2. front door
# Any answer but 404 (what the Gateway gives a hostname no route takes) and 5xx: the request
# reached the app.
hosts=$(kubectl -n "$ns" get httproute -o jsonpath='{range .items[*]}{range .spec.hostnames[*]}{@}{"\n"}{end}{end}')
for h in $hosts; do
  code=""
  for _ in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 -H "Host: $h" "$FRONT_DOOR/" || true)
    [[ "$code" =~ ^[234][0-9][0-9]$ && "$code" != 404 ]] && break
    sleep 2
  done
  [[ "$code" =~ ^[234][0-9][0-9]$ && "$code" != 404 ]] || die "front door: http://$h/ answered ${code:-nothing}"
  pass "front door: http://$h/ answers $code"
done
[[ -n "$hosts" ]] || echo "skip front door: no HTTPRoute in $ns"

# ---------------------------------------------------------------- 3. metrics targets up
# Prometheus Operator names each monitor's scrape pool <serviceMonitor|podMonitor>/<ns>/<name>/<n>.
monitors=$(kubectl -n "$ns" get servicemonitor,podmonitor -o json 2>/dev/null \
  | jq -r '.items[] | "\(.kind[:1] | ascii_downcase)\(.kind[1:])/\(.metadata.namespace)/\(.metadata.name)/"' || true)
for m in $monitors; do
  state=""
  for _ in $(seq 1 60); do # a new monitor reaches Prometheus' config within a minute or two
    state=$(kubectl get --raw "$PROM/api/v1/targets?state=active" | jq -r --arg m "$m" \
      '[.data.activeTargets[] | select(.scrapePool | startswith($m)) | .health] | "\(length) \(unique | join(","))"')
    [[ "$state" == "0 " || "$state" != *" up" ]] || break
    sleep 3
  done
  [[ "$state" != "0 " && "$state" == *" up" ]] || die "metrics: $m targets (count, health): ${state:-none}"
  pass "metrics: ${state%% *} targets of $m up"
done
[[ -n "$monitors" ]] || echo "skip metrics: no ServiceMonitor or PodMonitor in $ns"

# ---------------------------------------------------------------- 4. rules loaded
groups=$(kubectl -n "$ns" get prometheusrule -o json 2>/dev/null | jq -r '.items[].spec.groups[].name' || true)
if [[ -n "$groups" ]]; then
  missing=""
  for _ in $(seq 1 60); do
    loaded=$(kubectl get --raw "$PROM/api/v1/rules" | jq -r '.data.groups[].name')
    missing=$(comm -23 <(echo "$groups" | sort -u) <(echo "$loaded" | sort -u) | xargs)
    [[ -n "$missing" ]] || break
    sleep 3
  done
  [[ -z "$missing" ]] || die "rules: Prometheus hasn't loaded $missing"
  pass "rules: Prometheus has $(echo "$groups" | wc -l | tr -d ' ') groups of $ns's PrometheusRules"
else
  echo "skip rules: no PrometheusRule in $ns"
fi

pass "$app in $env on the platform at this commit"
