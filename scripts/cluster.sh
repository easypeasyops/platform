#!/usr/bin/env bash
# Local Kubernetes for shortly: one kind cluster, two namespaces (staging, prod), each with
# its own database — the shape of real environments, on a laptop.
#
#   scripts/cluster.sh up              # idempotent
#   MONITORING=0 scripts/cluster.sh up # skip kube-prometheus-stack (saves ~1 GB of RAM)
#   POLICIES=0 scripts/cluster.sh up   # skip Kyverno and the admission policies
#   BACKUPS=0 scripts/cluster.sh up    # skip the backup bucket (the databases can't archive)
#   ARGOCD_APPS=0 scripts/cluster.sh up # don't let Argo CD manage the app (CI: the e2e
#                                       # deploys with deploy.sh instead)
#   PLATFORM_REVISION=<sha> scripts/cluster.sh up # the platform from that commit, not main
#   FRONT_DOOR=0 scripts/cluster.sh up  # no forwarders on the laptop's ports 8080 and 8090
#   scripts/cluster.sh wake            # after the laptop slept: restart the CronJob
#                                       # scheduler, so the cleanup CronJob runs on time
#   scripts/cluster.sh down
#
# What runs where:
#   - Argo CD: bootstrapped here with Helm, the one component not deployed from git.
#   - The platform (Envoy Gateway and the Gateway, Argo Rollouts, metrics-server,
#     kube-prometheus-stack, Kyverno and its policies, the backup bucket, the tool routes):
#     Argo CD, from deploy/argocd/platform/ (app-of-apps). A merged change to it reaches the
#     cluster within a minute; this script only applies the root and waits for it.
#   - The platform's secrets: External Secrets, from the secret store (deploy/secrets/),
#     where each is generated once (platform plan 4.1). Nothing here makes them.
#   - Each environment's namespace (restricted Pod Security) and its default-deny network
#     policy (deploy/app-namespace/): here, and by Argo CD too once the apps are on (the
#     ApplicationSet creates the namespace and applies the same). Its database is the app's own (the service chart's database.cluster,
#     CloudNativePG; platform plan 4.3), with its credentials and backups: nothing here.
#   - The apps: Argo CD, from this repo's apps root (deploy/argocd/apps-root.yaml): its
#     ApplicationSet generates one Application per apps/<app>/envs/<env>.yaml, rendering
#     the service chart with values from the app's repo (shortly: easypeasyops/links).
set -euo pipefail

CLUSTER=shortly
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# ENVS, and the namespace each runs in (ns_of)
# shellcheck source=scripts/lib/namespaces.sh
source "$ROOT/scripts/lib/namespaces.sh"
# Argo CD: deploys the platform and the app from git. Pinned by version and digest: the
# tag says what it is, the digest that it can't change. (The platform's chart pins are in
# deploy/argocd/platform/values.yaml.)
ARGOCD_CHART=oci://ghcr.io/argoproj/argo-helm/argo-cd
ARGOCD_VERSION=10.11.1 # Argo CD v3.5.6
ARGOCD_DIGEST=sha256:2ac5224a5c201789f96aa9e085673d80732591d1b3e815a8a825a4db6d1d7c88
# The front door: one tiny socat container per port, beside the kind node, forwarding the
# laptop's 127.0.0.1:<host> to the Gateway's fixed node port (deploy/kind/gateway.yaml).
# A port-forward dies with its terminal, the laptop's sleep or the pod it pinned; these
# restart with Docker and outlive every release. Containers rather than kind's
# extraPortMappings, which only take effect when the cluster is created.
FRONT_DOOR_IMAGE=docker.io/alpine/socat:1.8.1.3@sha256:22d77becaeeea8ef56b8c4bb2312323acafe85354be6a85714ff5050e3537842
FRONT_DOOR_PORTS=("8080:30080" "8090:30081") # host:nodePort, users (http) and dev tools (tools)
# Releases scripts/cluster.sh used to install with Helm, now Argo CD's (namespace/release):
# their Helm records are removed once Argo CD has adopted the resources.
HELM_RELEASES=(monitoring/monitoring kyverno/kyverno envoy-gateway-system/eg argo-rollouts/argo-rollouts)

on() { [[ "${!1:-1}" != 0 ]]; } # on MONITORING: the knob is unset or anything but 0
bool() { if on "$1"; then echo true; else echo false; fi; }

up() {
  if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    kind create cluster --config "$ROOT/deploy/kind/cluster.yaml" --wait 120s
  fi
  kubectl config use-context "kind-$CLUSTER" >/dev/null
  install_argocd
  platform
  take_over_from_helm
  if on FRONT_DOOR; then front_door; fi

  for env in "${ENVS[@]}"; do app_namespace "$(ns_of "$env")"; done
  echo "cluster ready: namespaces $STAGING_NS $PROD_NS"

  # Last, once the namespaces exist: from here on, git decides what runs.
  if on ARGOCD_APPS; then
    take_over_apps
    kubectl apply -f "$APPS_ROOT/apps-project.yaml" -f "$APPS_ROOT/apps-root.yaml" >/dev/null
    for env in "${ENVS[@]}"; do wait_synced "shortly-$env" 300; done
    kubectl -n argocd get applications
  fi
}

# One environment's namespace: restricted Pod Security and default-deny ingress. Idempotent.
# Its database, the database's credentials and backups come with the app (the service
# chart's database.cluster); the old Postgres, its Secrets and its pg_dump CronJob were made
# here until platform plan 4.3 (RUNBOOK in links: removing them from a cluster made before).
app_namespace() {
  local ns=$1
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  # Enforce the "restricted" Pod Security Standard: the API server itself rejects any pod
  # that runs as root, keeps capabilities, or allows privilege escalation.
  kubectl label namespace "$ns" --overwrite \
    pod-security.kubernetes.io/enforce=restricted \
    pod-security.kubernetes.io/warn=restricted >/dev/null
  # Default-deny ingress. The chart adds the app's own policies (the app's, its database's).
  kubectl -n "$ns" apply -f "$ROOT/deploy/app-namespace/default-deny.yaml" >/dev/null
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
  return 1
}

# Apply the platform's root Application and wait until every child is Synced and Healthy.
# Its Helm parameters are what git can't know: the node's architecture, which commit to
# deploy, and which components this cluster wants.
platform() {
  local arch revision params children
  arch=$(kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.architecture}')
  revision=${PLATFORM_REVISION:-main}
  params=$(printf '[{"name":"arch","value":"%s"},{"name":"revision","value":"%s"},{"name":"components.monitoring","value":"%s"},{"name":"components.policies","value":"%s"},{"name":"components.backups","value":"%s"}]' \
    "$arch" "$revision" "$(bool MONITORING)" "$(bool POLICIES)" "$(bool BACKUPS)")
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
    | REVISION="$revision" PARAMS="$params" python3 -c '
import json, os, sys
app = json.load(sys.stdin)
app["spec"]["source"]["targetRevision"] = os.environ["REVISION"]
app["spec"]["source"]["helm"]["parameters"] = json.loads(os.environ["PARAMS"])
json.dump(app, sys.stdout)' \
    | kubectl apply --server-side --field-manager=cluster-sh --force-conflicts -f - >/dev/null
  echo "platform: syncing deploy/argocd/platform at $revision ($arch) through Argo CD"
  # The root is Healthy once every wave is: Argo CD reports each child's health
  # (argocd-cm, deploy/kind/argocd-values.yaml) and syncs wave 1 only after wave 0.
  wait_synced platform 600 || exit 1
  children=$(kubectl -n argocd get applications -l app.kubernetes.io/part-of=platform -o name)
  for app in $children; do wait_synced "${app#*/}" 300 || exit 1; done
  kubectl -n argocd get applications -l app.kubernetes.io/part-of=platform
  # Argo CD's health says the pods run; these say they do their job.
  kubectl -n gateway wait gateway/shortly --for=condition=Programmed --timeout=180s >/dev/null
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
# resources. Once Argo CD manages them, the Helm records only mislead (helm list would show
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

install_argocd() {
  kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl label namespace argocd --overwrite pod-security.kubernetes.io/enforce=restricted >/dev/null
  if ! helm upgrade --install argocd "$ARGOCD_CHART@$ARGOCD_DIGEST" --version "$ARGOCD_VERSION" \
    --namespace argocd --values "$ROOT/deploy/kind/argocd-values.yaml" \
    --wait --timeout 5m >/dev/null; then
    kubectl -n argocd get pods -o wide >&2 || true
    kubectl -n argocd get events --sort-by=.lastTimestamp 2>/dev/null | grep -iE 'fail|error|back-off' | tail -10 >&2 || true
    return 1
  fi
  echo "argocd ready: Argo CD (chart $ARGOCD_VERSION) in namespace argocd"
}

# Start (or replace) the front-door forwarders. Not fatal: the cluster works without them,
# through kubectl port-forward (RUNBOOK, Local cluster operations).
front_door() {
  local node="$CLUSTER-control-plane" mapping host node_port name
  for mapping in "${FRONT_DOOR_PORTS[@]}"; do
    host=${mapping%%:*} node_port=${mapping##*:} name="$CLUSTER-front-door-$host"
    docker rm -f "$name" >/dev/null 2>&1 || true
    # 127.0.0.1 only: the tools listener has no login of its own (Prometheus, Alertmanager).
    if ! docker run -d --name "$name" --restart unless-stopped --network kind \
      -p "127.0.0.1:$host:$host" "$FRONT_DOOR_IMAGE" \
      "tcp-listen:$host,fork,reuseaddr" "tcp-connect:$node:$node_port" >/dev/null; then
      docker rm -f "$name" >/dev/null 2>&1 || true
      echo "WARNING: front door: port $host is taken on this machine (an old kubectl port-forward?):" >&2
      lsof -nP -iTCP:"$host" -sTCP:LISTEN >&2 2>/dev/null || true
      echo "  stop it and rerun scripts/cluster.sh up" >&2
    fi
  done
  echo "front door: http://sho.rt.localhost:8080 (prod), http://staging.sho.rt.localhost:8080,"
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
  kind delete cluster --name "$CLUSTER"
}

case "${1:-}" in
  up) up ;;
  wake) wake ;;
  down) down ;;
  *) echo "usage: $0 up|wake|down" >&2; exit 2 ;;
esac
