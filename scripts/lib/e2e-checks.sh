# shellcheck shell=bash
# The e2e's checks on one app namespace, on whatever cluster the current kube context is:
# kind's e2e (scripts/kind-e2e.sh) runs them after deploying the app itself, and the weekly
# GKE run (PT-120) on each namespace Argo CD deployed. One copy, so both judge the same.
#
#   e2e_checks <namespace> <front door URL>   # e.g. http://127.0.0.1:8080, the users port
#   e2e_diagnostics <namespace> <app>         # the namespace's state, for a failed run
#
# The checks: every Deployment, StatefulSet and Rollout Ready; the front door routes each
# HTTPRoute hostname to the app; Prometheus scrapes every target of each ServiceMonitor and
# PodMonitor, all up; Prometheus has loaded each PrometheusRule's groups. A failed check
# exits (die).
PROM=/api/v1/namespaces/monitoring/services/http:kps-prometheus:9090/proxy

pass() { echo "ok   $* (${SECONDS}s)"; }
die() { echo "FAIL: $*" >&2; exit 1; }

e2e_diagnostics() {
  local ns=$1 app=$2
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

e2e_checks() {
  local ns=$1 front_door=$2
  local workloads rollouts r w all hosts h code monitors m state groups missing loaded

  # -------------------------------------------------------------- 1. workloads ready
  workloads=$(kubectl -n "$ns" get deployment,statefulset -o name)
  for w in $workloads; do kubectl -n "$ns" rollout status "$w" --timeout=180s >/dev/null; done
  rollouts=$(kubectl -n "$ns" get rollouts.argoproj.io -o name 2>/dev/null || true)
  for r in $rollouts; do
    kubectl -n "$ns" wait "$r" --for=jsonpath='{.status.phase}'=Healthy --timeout=300s >/dev/null
  done
  all=$(echo "$workloads $rollouts" | xargs)
  [[ -n "$all" ]] || die "no Deployment, StatefulSet or Rollout in $ns"
  pass "ready: $all"

  # -------------------------------------------------------------- 2. front door
  # Any answer but 404 (what the Gateway gives a hostname no route takes) and 5xx: the
  # request reached the app.
  hosts=$(kubectl -n "$ns" get httproute -o jsonpath='{range .items[*]}{range .spec.hostnames[*]}{@}{"\n"}{end}{end}')
  for h in $hosts; do
    code=""
    for _ in $(seq 1 30); do
      code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 -H "Host: $h" "$front_door/" || true)
      [[ "$code" =~ ^[234][0-9][0-9]$ && "$code" != 404 ]] && break
      sleep 2
    done
    [[ "$code" =~ ^[234][0-9][0-9]$ && "$code" != 404 ]] || die "front door: http://$h/ answered ${code:-nothing}"
    pass "front door: http://$h/ answers $code"
  done
  [[ -n "$hosts" ]] || echo "skip front door: no HTTPRoute in $ns"

  # -------------------------------------------------------------- 3. metrics targets up
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

  # -------------------------------------------------------------- 4. rules loaded
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
}
