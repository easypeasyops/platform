#!/usr/bin/env bash
# Local Kubernetes for shortly: one kind cluster, two namespaces (staging, prod), each with
# its own database — the shape of real environments, on a laptop.
#
#   scripts/cluster.sh up              # idempotent: create, then install
#   scripts/cluster.sh create          # the kind cluster, its load balancer and its front
#                                       # door, nothing on it
#   scripts/cluster.sh install         # the platform and the apps, on the current kube
#                                       # context: any cluster, not only kind
#   CONTEXT=<name> scripts/cluster.sh install # on that context (it becomes the current one)
#   MONITORING=0 scripts/cluster.sh up # skip kube-prometheus-stack, node-exporter, VictoriaLogs and the log collector (saves ~1 GB of RAM)
#   POLICIES=0 scripts/cluster.sh up   # skip Kyverno and the admission policies
#   BACKUPS=0 scripts/cluster.sh up    # skip the backup bucket (the databases can't archive)
#   SLACK=0 scripts/cluster.sh up      # alerts stay in Alertmanager. With Slack on (the
#                                       # default, with monitoring), no bot token in Keychain
#                                       # or the store is an error: `up` brings up the rest,
#                                       # then exits non-zero (RUNBOOK, Slack alerts)
#   ARGOCD_APPS=0 scripts/cluster.sh up # don't let Argo CD manage the app (CI: the e2e
#                                       # deploys with deploy.sh instead)
#   PLATFORM_REVISION=<sha> scripts/cluster.sh up # the platform from that commit, not main
#   FRONT_DOOR=0 scripts/cluster.sh up  # no forwarders on the laptop's ports 8080 and 8090
#                                       # (create's knob)
#   PROFILE=<name> scripts/cluster.sh install # the cluster's values over kind's, from
#                                       # deploy/profiles/<name>/ (gke: a create step's
#                                       # cluster; DESIGN, Profiles). Unset: kind's alone
#   ARGOCD_HELM=1 scripts/cluster.sh install # re-install Argo CD with Helm though it
#                                       # manages itself: when a change broke it (RUNBOOK,
#                                       # Argo CD broke itself)
#   scripts/cluster.sh wake            # after the laptop slept: restart the CronJob
#                                       # scheduler, so the cleanup CronJob runs on time
#   scripts/cluster.sh down
#
# What runs where:
#   - Argo CD: bootstrapped here with Helm, once; then its own `argocd` Application (in the
#     platform chart) owns it, from the same pin and values file, like the rest.
#   - The platform (Envoy Gateway and the Gateway, Argo Rollouts, metrics-server,
#     kube-prometheus-stack, node-exporter, VictoriaLogs and its log collector, Kyverno and its policies, the backup bucket, the tool routes):
#     Argo CD, from deploy/argocd/platform/ (app-of-apps). A merged change to it reaches the
#     cluster within a minute; this script only applies the root and waits for it.
#   - The platform's secrets: External Secrets, from the secret store (deploy/secrets/),
#     where each is generated once (platform plan 4.1). Nothing here makes them.
#   - The apps: Argo CD, from this repo's apps root (deploy/argocd/apps-root.yaml): its
#     ApplicationSet generates one Application per apps/<app>/envs/<env>.yaml, rendering
#     the service chart with values from the app's repo (shortly: easypeasyops/links).
#     Each Application creates its namespace (restricted Pod Security) and applies its
#     baseline (deploy/app-namespace/: default-deny); the app's database comes with it (the
#     service chart's database.cluster, CloudNativePG). Nothing here makes either.
set -euo pipefail

CLUSTER=shortly
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# ENVS: the environments, whose old Applications take_over_apps() hands over
# shellcheck source=scripts/lib/namespaces.sh
source "$ROOT/scripts/lib/namespaces.sh"
# Slack's bot token, copied from Keychain into the store by platform()
# shellcheck source=scripts/lib/slack.sh
source "$ROOT/scripts/lib/slack.sh"
# Argo CD: deploys the platform and the app from git. Pinned by version and digest beside
# the platform's other chart pins (deploy/argocd/platform/values.yaml, charts.argocd, which
# Renovate bumps): the tag says what it is, the digest that it can't change.
read -r ARGOCD_CHART ARGOCD_VERSION ARGOCD_DIGEST < <(awk '
  /^charts:/ { charts = 1 }
  charts && /^  argocd:/ { pin = 1; next }
  pin && $1 == "repo:" { repo = $2 }
  pin && $1 == "version:" { version = $2 }
  pin && $1 == "digest:" { print repo, version, $2; exit }' "$ROOT/deploy/argocd/platform/values.yaml")
# The front door: one tiny socat container per port, beside the kind node, forwarding the
# laptop's 127.0.0.1:<host> to the Gateway's load balancer (below), as a cloud's DNS name
# points at its load balancer's address. On macOS the kind network can't be reached from
# the laptop, and cloud-provider-kind publishes a load balancer only on random host ports:
# these give it fixed ones. They reach it by its container's name, which cloud-provider-kind
# derives from the Service (kindccm- and a hash of cluster/namespace/name) and Docker's DNS
# resolves on each connection, so they outlive a new load balancer with a new address.
# A port-forward dies with its terminal, the laptop's sleep or the pod it pinned; these
# restart with Docker and outlive every release.
FRONT_DOOR_IMAGE=docker.io/alpine/socat:1.8.1.3@sha256:22d77becaeeea8ef56b8c4bb2312323acafe85354be6a85714ff5050e3537842
FRONT_DOOR_SERVICE=envoy-gateway-system/front-door # the proxies' Service (deploy/kind/gateway.yaml)
FRONT_DOOR_PORTS=("8080:80" "8090:8081") # host:Service port, users (http) and dev tools (tools)
# The cluster's LoadBalancer implementation, as a cloud has one: kubernetes-sigs'
# cloud-provider-kind, a container beside the node that gives each LoadBalancer Service an
# address on the kind network, served by an Envoy container of its own (kindccm-*). It
# drives Docker through the socket, as kind does. Its Gateway API and Ingress controllers
# are off: Envoy Gateway is the platform's, and owns the Gateway API CRDs.
LOAD_BALANCER=$CLUSTER-cloud-provider-kind
LOAD_BALANCER_IMAGE=registry.k8s.io/cloud-provider-kind/cloud-controller-manager:v0.12.0@sha256:1b97632e5dfec8f83c7e970cf18cedb9e6b707e272011d712cabf6ab59d323c9
# Releases scripts/cluster.sh installs (or used to) with Helm, now Argo CD's
# (namespace/release): their Helm records are removed once Argo CD has adopted the
# resources. Argo CD's own (argocd/argocd) is installed again on each fresh cluster.
HELM_RELEASES=(monitoring/monitoring kyverno/kyverno envoy-gateway-system/eg argo-rollouts/argo-rollouts argocd/argocd)

on() { [[ "${!1:-1}" != 0 ]]; } # on MONITORING: the knob is unset or anything but 0
bool() { if on "$1"; then echo true; else echo false; fi; }
SLACK_MISSING=0 # set by platform(): Slack on, but no bot token anywhere

up() {
  create
  CONTEXT="kind-$CLUSTER" install
}

# What only kind needs: the cluster itself, its LoadBalancer implementation, and the
# forwarders to the laptop's ports. Nothing on the cluster is installed here.
create() {
  if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    kind create cluster --config "$ROOT/deploy/kind/cluster.yaml" --wait 120s
  fi
  kubectl config use-context "kind-$CLUSTER" >/dev/null
  load_balancer
  if on FRONT_DOOR; then front_door; fi
}

# The platform and the apps, on any cluster: the kube context CONTEXT, or the current one.
# Nothing here names kind.
install() {
  CONTEXT=${CONTEXT:-$(kubectl config current-context)}
  kubectl config use-context "$CONTEXT" >/dev/null
  trap startup_report EXIT # on success and on failure
  install_argocd
  platform
  take_over_from_helm

  echo "cluster ready"

  # Last: from here on, git decides what runs. Each app's Application creates its namespace
  # (apps/applicationset.yaml). With ARGOCD_APPS=0 nothing does: whatever deploys the app
  # makes it (links' e2e does, as the ApplicationSet would).
  if on ARGOCD_APPS; then
    take_over_apps
    kubectl apply -f "$APPS_ROOT/apps-project.yaml" -f "$APPS_ROOT/apps-root.yaml" >/dev/null
    for f in "$ROOT"/apps/*/envs/*.yaml; do # one Application per file: <app>-<env>
      wait_synced "$(basename "$(dirname "$(dirname "$f")")")-$(basename "$f" .yaml)" 300
    done
    kubectl -n argocd get applications
  fi

  # Last of all, so everything else is up: Slack on without its token is an error.
  if ((SLACK_MISSING)); then
    slack_token_missing
    exit 1
  fi
}

# Wait for an Application to be Synced AND Healthy. Healthy alone comes first when Argo CD
# adopts resources that already run, before its first sync has applied git (seen on the
# first takeover: OutOfSync/Healthy). On timeout, say what is out of sync, and fail.
wait_synced() {
  local app=$1 timeout=$2 state="" t0=$SECONDS last=$SECONDS
  # A fresh comparison first. Right after its spec changes (the root at a new revision, a
  # child the root just rewrote), an Application still shows its last comparison, often
  # Synced/Healthy, for a few seconds: this wait used to pass on that and report "platform
  # ready" before Argo CD had looked at the new commit. So ask Argo CD to refresh, and
  # wait until it has (it removes the annotation when done). An Application that doesn't
  # exist yet has no stale state: skip.
  if kubectl -n argocd annotate application "$app" argocd.argoproj.io/refresh=normal \
    --overwrite >/dev/null 2>&1; then
    for _ in $(seq 1 30); do
      [[ -z "$(kubectl -n argocd get application "$app" \
        -o jsonpath='{.metadata.annotations.argocd\.argoproj\.io/refresh}' 2>/dev/null)" ]] && break
      sleep 1
    done
  fi
  while ((SECONDS - t0 < timeout)); do
    state=$(kubectl -n argocd get application "$app" \
      -o jsonpath='{.status.sync.status}/{.status.health.status}' 2>/dev/null || true)
    [[ "$state" == Synced/Healthy ]] && return 0
    if ((SECONDS - last >= 30)); then # progress, so a slow sync shows where it is
      last=$SECONDS
      # Each Application's state, its sync operation (phase, retries), and for one that
      # isn't synced or healthy, what holds it back.
      echo "  ... $app is ${state:-not there yet} after $((SECONDS - t0))s; $(kubectl -n argocd get applications \
        -o jsonpath='{range .items[*]}{.metadata.name}={.status.sync.status}/{.status.health.status} (op {.status.operationState.phase} #{.status.operationState.retryCount}){range .status.resources[?(@.status=="OutOfSync")]} [OutOfSync {.kind}/{.name}]{end}{range .status.resources[?(@.health.status=="Degraded")]} [Degraded {.kind}/{.name}: {.health.message}]{end}{range .status.resources[?(@.health.status=="Progressing")]} [Progressing {.kind}/{.name}]{end} {end}' 2>/dev/null)"
      # The API server's own view, to compare with Argo CD's (whose health can lag).
      echo "      API server: $(kubectl get crd -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Established")].status}{"\n"}{end}' 2>/dev/null | grep -vc True || true) CRDs not yet Established"
    fi
    sleep 3
  done
  echo "FAIL: Argo CD application $app is ${state:-missing} after ${timeout}s:" >&2
  kubectl -n argocd get application "$app" -o jsonpath='{range .status.resources[?(@.status!="Synced")]}  out of sync: {.kind}/{.name}{"\n"}{end}{range .status.resources[?(@.health.status!="Healthy")]}  {.health.status}: {.kind}/{.name} {.health.message}{"\n"}{end}{range .status.conditions[*]}  {.type}: {.message}{"\n"}{end}{.status.operationState.message}{"\n"}' >&2 || true
  # The API server's view of each resource Argo CD calls unhealthy: conditions there that
  # Argo CD doesn't show mean its cached copy is stale (argocd-values.yaml).
  local kind ns name
  while IFS='|' read -r kind ns name; do
    echo "  API server, ${kind%.}/$name: $(kubectl get "${kind%.}" "$name" ${ns:+-n "$ns"} \
      -o jsonpath='{range .status.conditions[*]}{.type}={.status} {end}' 2>&1)" >&2
  done < <(kubectl -n argocd get application "$app" -o jsonpath='{range .status.resources[?(@.health.status!="Healthy")]}{.kind}.{.group}|{.namespace}|{.name}{"\n"}{end}' 2>/dev/null)
  return 1
}

# Where a start-up's time went, per pod: seconds from the pod's creation to scheduled,
# initialized, its containers started and Ready, and the seconds its image pulls took
# (kubelet's Pulled events; pulls overlap, so they don't add up to wall time). Slowest
# first, then the node's CPU requests and use: on CI's 4-CPU node, a pod that waits for CPU
# shows as a late "sched". Printed when `up` ends, failed or not; never fails it.
startup_report() {
  echo "== start-up, seconds after each pod's creation, pods of the last hour (slowest to Ready first)"
  python3 - <<'EOF' || true
import json, re, subprocess, time
from datetime import datetime

def get(*args):
    return json.loads(subprocess.run(["kubectl", "get", *args, "-o", "json"], capture_output=True,
                                     text=True, check=True).stdout)["items"]

def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

def secs(d):  # kubelet's durations: 850ms, 3.2s, 1m2.5s
    m = re.fullmatch(r"(?:(\d+)m(?!s))?([\d.]+)(ms|s)", d)
    return 0.0 if not m else int(m[1] or 0) * 60 + float(m[2]) / (1000 if m[3] == "ms" else 1)

pulls, size = {}, 0
for e in get("events", "-A", "--field-selector", "reason=Pulled"):
    m = re.search(r" in ([\dmsh.]+) .*Image size: (\d+) bytes", e.get("message", ""))
    if m:
        key = f"{e['involvedObject'].get('namespace')}/{e['involvedObject']['name']}"
        pulls[key] = pulls.get(key, 0.0) + secs(m[1])
        size += int(m[2])

rows, cpu = [], 0
for p in get("pods", "-A"):
    if p["status"].get("phase") in ("Succeeded", "Failed"):
        continue
    for c in p["spec"]["containers"]:
        r = c.get("resources", {}).get("requests", {}).get("cpu", "0")
        cpu += int(r[:-1]) if r.endswith("m") else int(float(r) * 1000)
    t0, cond = ts(p["metadata"]["creationTimestamp"]), {}
    if time.time() - t0 > 3600:  # not from this start-up (the laptop's long-running pods)
        continue
    for c in p["status"].get("conditions", []):
        if c["status"] == "True" and c.get("lastTransitionTime"):
            cond[c["type"]] = ts(c["lastTransitionTime"]) - t0
    started = [ts(s["state"]["running"]["startedAt"]) - t0
               for s in p["status"].get("containerStatuses", []) if "running" in s.get("state", {})]
    name = f"{p['metadata']['namespace']}/{p['metadata']['name']}"
    rows.append((cond.get("Ready", 1e9), cond.get("PodScheduled"), cond.get("Initialized"),
                 pulls.get(name), max(started) if started else None, name))

f = lambda v: "    -" if v is None or v >= 1e9 else f"{v:5.0f}"
print("  sched  init  pull  start  ready  pod")
for ready, sched, init, pull, start, name in sorted(rows, key=lambda r: -r[0])[:15]:
    print(f"  {f(sched)} {f(init)} {f(pull)}  {f(start)}  {f(ready)}  {name}")
print(f"  {len(pulls)} pods pulled {size / 1e9:.1f} GB in {sum(pulls.values()):.0f} s of pulls")
node = get("nodes")[0]["status"]["allocatable"]["cpu"]
alloc = int(node[:-1]) if node.endswith("m") else int(node) * 1000
print(f"== node: CPU requested {cpu}m of {alloc}m allocatable ({100 * cpu // alloc}%)")
EOF
  kubectl top node 2>/dev/null || echo "  (no kubectl top: metrics-server not serving yet)"
}

# Apply the platform's root Application and wait until every child is Synced and Healthy.
# Its Helm parameters are what git can't know: the node's architecture, which commit to
# deploy, and which components this cluster wants.
platform() {
  local arch revision found slack=false params children
  arch=$(kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.architecture}')
  revision=${PLATFORM_REVISION:-main}
  # Slack (on unless SLACK=0; part of monitoring) only where the store has the bot token
  # (RUNBOOK, Slack alerts): without it the monitoring Application would wait for good on an
  # ExternalSecret that can't sync. Where Keychain has the token (scripts/load-slack.sh), it
  # goes into the store first, so a new cluster comes up with Slack. Where neither has it,
  # the rest comes up without Slack and `up` fails at the end (SLACK_MISSING): a new cluster
  # needs to be up before the token can be loaded. A failed lookup stops here (set -e).
  if on MONITORING && on SLACK; then
    slack_token_to_store "$CONTEXT"
    found=$(slack_token_in_store "$CONTEXT")
    if [[ -n "$found" ]]; then slack=true; else SLACK_MISSING=1; fi
  fi
  # components.argocd: Argo CD manages its own install from here on (PT-109).
  params=$(printf '[{"name":"arch","value":"%s"},{"name":"revision","value":"%s"},{"name":"components.monitoring","value":"%s"},{"name":"components.slack","value":"%s"},{"name":"components.policies","value":"%s"},{"name":"components.backups","value":"%s"},{"name":"components.argocd","value":"true"}]' \
    "$arch" "$revision" "$(bool MONITORING)" "$slack" "$(bool POLICIES)" "$(bool BACKUPS)")
  # A profile: the root's parameter `profile` (each child adds its overlay) and the
  # profile's own values for the root, which must exist.
  if [[ -n "${PROFILE:-}" && ! -f "$ROOT/deploy/profiles/$PROFILE/platform.yaml" ]]; then
    echo "PROFILE=$PROFILE: no deploy/profiles/$PROFILE/platform.yaml" >&2
    exit 2
  fi
  # The project, to bootstrap: the root's platform-project child keeps it in sync from git
  # after that (wave -2).
  kubectl apply -f "$ROOT/deploy/argocd/platform-project.yaml" >/dev/null
  # One apply with the parameters already set: applying the file as it is, then patching,
  # would first sync main on the wrong architecture.
  # Rendered from the file alone (create, not apply: no live object merged in), and applied
  # server-side: a client-side apply diffs against the last-applied annotation, which an
  # earlier version of this function filled with server fields, so every re-run asked to
  # delete metadata.resourceVersion and was refused.
  kubectl create -f "$ROOT/deploy/argocd/platform-root.yaml" --dry-run=client -o json \
    | REVISION="$revision" PARAMS="$params" PROFILE="${PROFILE:-}" python3 -c '
import json, os, sys
app = json.load(sys.stdin)
app["spec"]["source"]["targetRevision"] = os.environ["REVISION"]
app["spec"]["source"]["helm"]["parameters"] = json.loads(os.environ["PARAMS"])
if os.environ["PROFILE"]:
    helm = app["spec"]["source"]["helm"]
    helm["parameters"].append({"name": "profile", "value": os.environ["PROFILE"]})
    helm["valueFiles"].append("../../profiles/%s/platform.yaml" % os.environ["PROFILE"])
json.dump(app, sys.stdout)' \
    | kubectl apply --server-side --field-manager=cluster-sh --force-conflicts -f - >/dev/null
  echo "platform: syncing deploy/argocd/platform at $revision ($arch) through Argo CD"
  # The root is Healthy once every wave is: Argo CD reports each child's health
  # (argocd-cm, deploy/kind/argocd-values.yaml) and syncs wave 1 only after wave 0.
  # 15 minutes: on a fresh cluster Argo CD shows Envoy Gateway's CRDs OutOfSync, then not
  # Established, for up to about 10 minutes after the API server has established them (its
  # cached copy lags; argocd-values.yaml). When that tail falls inside this wait, 600 s
  # wasn't enough (links #170, 2026-11-17; 555 s at the pin before).
  wait_synced platform 900 || exit 1
  children=$(kubectl -n argocd get applications -l app.kubernetes.io/part-of=platform -o name)
  # Side by side: each wait asks for a refresh, waits for it, then polls every 3 s. One
  # after another, those add up even when every child is already Synced/Healthy (21
  # children took 80 s after the root on links #171); side by side, the whole takes the
  # slowest child's time.
  local pids=() failed=0 pid
  for app in $children; do wait_synced "${app#*/}" 300 & pids+=("$!"); done
  for pid in "${pids[@]}"; do wait "$pid" || failed=1; done
  ((failed == 0)) || exit 1
  kubectl -n argocd get applications -l app.kubernetes.io/part-of=platform
  # Argo CD's health says the pods run; these say they do their job.
  kubectl -n gateway wait gateway/front-door --for=condition=Programmed --timeout=180s >/dev/null
  if on POLICIES; then
    # A policy only protects once Kyverno has compiled it and registered its webhook.
    for kind in imagevalidatingpolicy validatingpolicy; do
      kubectl wait "$kind" --all --for=jsonpath='{.status.conditionStatus.ready}'=true \
        --timeout=120s >/dev/null
    done
  fi
  echo "platform ready: $(echo "$children" | wc -l | tr -d ' ') Applications Synced and Healthy"
}

# A cluster built before the platform moved under Argo CD has Helm releases for the same
# resources, and every cluster has Argo CD's own once install_argocd bootstrapped it. Once Argo CD manages them, the Helm records only mislead (helm list would show
# releases nobody upgrades): remove them. The resources stay; Argo CD owns them now.
take_over_from_helm() {
  local entry ns release
  for entry in "${HELM_RELEASES[@]}"; do
    ns=${entry%/*} release=${entry#*/}
    if kubectl -n "$ns" get secret -l "owner=helm,name=$release" -o name 2>/dev/null | grep -q .; then
      kubectl -n "$ns" delete secret -l "owner=helm,name=$release" >/dev/null
      echo "helm release $ns/$release handed over to Argo CD (its Helm records removed)"
    fi
  done
  drop_helm_field_ownership
}

# Helm installed those releases with server-side apply, so their objects' managedFields still
# hold a `helm` entry. The fields it owns outlive a removal from git: Argo CD drops only its
# own share, and a container Helm half-owns is left without an image (PT-104's Grafana).
# Remove that entry from every object of those releases (CRDs from a chart's crds/ carry no
# release annotation; Argo CD's are in its chart's templates/ and carry its own). A `test` on the manager guards each `remove` against a shifted index. Only field
# ownership changes: no spec, nothing restarts.
drop_helm_field_ownership() {
  local resource ns name patch removed=0
  while read -r resource ns name patch; do # ns is - for a cluster-scoped object
    [[ "$ns" == - ]] && ns=""
    # A failed test (the object changed since the list) leaves it for the next up.
    if kubectl ${ns:+-n "$ns"} patch "$resource/$name" --type=json -p "$patch" >/dev/null; then
      removed=$((removed + 1))
    fi
  done < <(kubectl get "$(kubectl api-resources --verbs=list,patch -o name | paste -sd, -)" -A \
    --show-managed-fields -o json 2>/dev/null \
    | RELEASES="${HELM_RELEASES[*]}" python3 -c '
import json, os, sys
releases = set(os.environ["RELEASES"].split())
namespaces = {r.split("/")[0] for r in releases}
for o in json.load(sys.stdin)["items"]:
    m = o["metadata"]
    a = m.get("annotations") or {}
    release = a.get("meta.helm.sh/release-namespace"), a.get("meta.helm.sh/release-name")
    if release[0] is None:  # a CRD from crds/: cluster-scoped, or in a release namespace
        if m.get("namespace", "-") not in namespaces | {"-"}:
            continue
    elif "/".join(release) not in releases:
        continue
    helm = [i for i, e in enumerate(m.get("managedFields") or []) if e.get("manager") == "helm"]
    patch = []
    for i in reversed(helm):  # last first, so the earlier indexes still hold
        path = f"/metadata/managedFields/{i}"
        patch += [{"op": "test", "path": path + "/manager", "value": "helm"},
                  {"op": "remove", "path": path}]
    if patch:
        group, _, version = o["apiVersion"].rpartition("/")
        resource = o["kind"] + (f".{version}.{group}" if group else "")
        print(resource, m.get("namespace", "-"), m["name"], json.dumps(patch, separators=(",", ":")))
')
  if ((removed)); then echo "helm field ownership removed from $removed objects (Argo CD owns them now)"; fi
}

# The apps root's files (deploy/argocd/apps-{project,root}.yaml).
APPS_ROOT="$ROOT/deploy/argocd"

# On a cluster set up before plan step 3.3, shortly-<env> were applied from links'
# deploy/argocd/applications.yaml. The platform's ApplicationSet generates Applications of
# the same names, and must own them: delete each one it doesn't own, which orphans its
# resources (nothing stops); the ApplicationSet's Application adopts them by name. Only an
# Application without a finalizer may go this way: with one, deleting it deletes the app.
take_over_apps() {
  local env app owner
  for env in "${ENVS[@]}"; do
    app="shortly-$env"
    kubectl -n argocd get application "$app" >/dev/null 2>&1 || continue
    owner=$(kubectl -n argocd get application "$app" -o jsonpath='{.metadata.ownerReferences[?(@.kind=="ApplicationSet")].name}')
    [[ "$owner" == apps ]] && continue
    if [[ -n "$(kubectl -n argocd get application "$app" -o jsonpath='{.metadata.finalizers}')" ]]; then
      echo "FAIL: Application $app has finalizers; deleting it would delete $app's resources." >&2
      echo "      Remove them first (RUNBOOK: Handing shortly to the platform's ApplicationSet)." >&2
      return 1
    fi
    kubectl -n argocd delete application "$app" --wait >/dev/null
    echo "Application $app handed over to the platform's ApplicationSet (its resources keep running)"
  done
}

# Argo CD, with Helm: on a fresh cluster (bootstrap), on one from before PT-109 (its
# Application adopts the release, take_over_from_helm hands the release over), and with
# ARGOCD_HELM=1 (recovery). Otherwise its `argocd` Application owns it: installing it here
# too would put this checkout's values over git's, maybe an older Argo CD. Over objects
# Argo CD owns, Helm takes them (--take-ownership: their release records are gone) and wins
# each field (--force-conflicts); the Application then syncs them back to git.
install_argocd() {
  kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl label namespace argocd --overwrite pod-security.kubernetes.io/enforce=restricted >/dev/null
  if [[ "${ARGOCD_HELM:-0}" == 0 ]] && kubectl -n argocd get application argocd >/dev/null 2>&1; then
    echo "argocd: managed by its own Application, not installed with Helm (ARGOCD_HELM=1 does)"
    return 0
  fi
  if ! helm upgrade --install argocd "$ARGOCD_CHART@$ARGOCD_DIGEST" --version "$ARGOCD_VERSION" \
    --namespace argocd --values "$ROOT/deploy/kind/argocd-values.yaml" \
    --server-side=true --force-conflicts --take-ownership \
    --wait --timeout 5m >/dev/null; then
    kubectl -n argocd get pods -o wide >&2 || true
    kubectl -n argocd get events --sort-by=.lastTimestamp 2>/dev/null | grep -iE 'fail|error|back-off' | tail -10 >&2 || true
    return 1
  fi
  echo "argocd ready: Argo CD (chart $ARGOCD_VERSION) in namespace argocd"
}

# Start cloud-provider-kind, or replace it when it runs another image; one that runs this
# image is left alone. Replaced with rm -f (SIGKILL): on a clean stop it deletes its load
# balancers' containers, which the new one would then have to make again.
load_balancer() {
  if [[ "$(docker inspect -f '{{.Config.Image}} {{.State.Running}}' "$LOAD_BALANCER" 2>/dev/null)" \
    == "$LOAD_BALANCER_IMAGE true" ]]; then
    return 0
  fi
  docker rm -f "$LOAD_BALANCER" >/dev/null 2>&1 || true
  docker run -d --name "$LOAD_BALANCER" --restart unless-stopped --network kind \
    --log-opt max-size=10m --log-opt max-file=2 \
    -v /var/run/docker.sock:/var/run/docker.sock "$LOAD_BALANCER_IMAGE" \
    --gateway-channel disabled >/dev/null
  echo "load balancer: cloud-provider-kind ($LOAD_BALANCER) serves LoadBalancer Services"
}

# Start (or replace) the front-door forwarders. Not fatal: the cluster works without them,
# through kubectl port-forward (RUNBOOK, Local cluster operations).
front_door() {
  local lb mapping host port name
  lb="kindccm-$(printf '%s' "$CLUSTER/$FRONT_DOOR_SERVICE" \
    | python3 -c 'import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:12])')"
  for mapping in "${FRONT_DOOR_PORTS[@]}"; do
    host=${mapping%%:*} port=${mapping##*:} name="$CLUSTER-front-door-$host"
    docker rm -f "$name" >/dev/null 2>&1 || true
    # 127.0.0.1 only: the tools listener has no login of its own (Prometheus, Alertmanager).
    if ! docker run -d --name "$name" --restart unless-stopped --network kind \
      -p "127.0.0.1:$host:$host" "$FRONT_DOOR_IMAGE" \
      "tcp-listen:$host,fork,reuseaddr" "tcp-connect:$lb:$port" >/dev/null; then
      docker rm -f "$name" >/dev/null 2>&1 || true
      echo "WARNING: front door: port $host is taken on this machine (an old kubectl port-forward?):" >&2
      lsof -nP -iTCP:"$host" -sTCP:LISTEN >&2 2>/dev/null || true
      echo "  stop it and rerun scripts/cluster.sh up" >&2
    fi
  done
  echo "front door: http://<app>.localhost:8080 (prod), http://staging.<app>.localhost:8080,"
  echo "  http://{argocd,grafana,prometheus,alertmanager}.localhost:8090"
}

# A sleeping Mac pauses Docker's VM, and the CronJob controller's timers only count time
# while it runs: at 10% awake, "the next run in an hour" comes 9 hours later (seen on
# 2026-10-24: the hourly backups at 22:41, 07:41, 16:41 and none between). Restarting the
# controller makes it read every CronJob (the app's cleanup) against the wall clock at
# once. The databases' backups don't depend on it: CloudNativePG schedules its own.
wake() {
  local node="$CLUSTER-control-plane" before
  kubectl config use-context "kind-$CLUSTER" >/dev/null
  before=$(kubectl -n kube-system get pod "kube-controller-manager-$node" \
    -o jsonpath='{.status.containerStatuses[0].restartCount}')
  # A static pod: stopping its container makes the kubelet start a fresh one.
  docker exec "$node" sh -c 'crictl stop $(crictl ps -q --name kube-controller-manager)' >/dev/null
  for _ in $(seq 1 60); do
    [[ "$(kubectl -n kube-system get pod "kube-controller-manager-$node" \
      -o jsonpath='{.status.containerStatuses[0].restartCount}/{.status.containerStatuses[0].ready}' 2>/dev/null)" \
      == "$((before + 1))/true" ]] && break
    sleep 2
  done
  echo "kube-controller-manager restarted (restarts: $((before + 1))): CronJobs rescheduled from the wall clock"
}

down() {
  local mapping
  for mapping in "${FRONT_DOOR_PORTS[@]}"; do
    docker rm -f "$CLUSTER-front-door-${mapping%%:*}" >/dev/null 2>&1 || true
  done
  # A clean stop, so cloud-provider-kind deletes its load balancers' containers as it exits;
  # then any it left.
  docker stop -t 30 "$LOAD_BALANCER" >/dev/null 2>&1 || true
  docker rm -f "$LOAD_BALANCER" >/dev/null 2>&1 || true
  docker ps -aq --filter "label=io.x-k8s.cloud-provider-kind.cluster=$CLUSTER" | xargs docker rm -f >/dev/null 2>&1 || true
  kind delete cluster --name "$CLUSTER"
}

case "${1:-}" in
  up) up ;;
  create) create ;;
  install) install ;;
  wake) wake ;;
  down) down ;;
  *) echo "usage: $0 up|create|install|wake|down" >&2; exit 2 ;;
esac
